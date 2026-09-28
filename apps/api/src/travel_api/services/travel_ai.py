from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any
from uuid import UUID

from travel_api.domain.models import (
    AIChatRequest,
    AIChatResult,
    ConversationMessage,
    ConversationRole,
    Trip,
)
from travel_api.services.ai_contracts import AIProvider, AIRequest
from travel_api.services.ai_orchestrator import ToolOrchestrationResult, ToolOrchestrator
from travel_api.services.conversation_context import ConversationContextBuilder
from travel_api.services.requirement_agent import RequirementAgent, TripRequirement
from travel_api.services.tool_registry import ToolRegistry
from travel_api.services.trip_service import TripNotFoundError, TripService

POLICY = """You are a fact-grounded AI Travel Planning Assistant.
Use only user-provided facts, the current Trip, and structured tool results.
Do not invent hotels, prices, ratings, opening hours, availability, distance, or currency.
Money must retain observed/estimated status and source references.
Use Decision, Budget, Freshness, and Route tools instead of recomputing their results.
Read tools are safe by default. Write tools require explicit user intent.
If a fact is missing or stale, say so and ask for clarification or refresh it.
"""

logger = logging.getLogger(__name__)

#: Verbs that signal a request to change the trip.
_ACTION_VERBS = "换|改|降低|增加|删除|移除|调整|刷新|重新规划|替换|取消|加上|去掉"
_ACTION_RE = re.compile(_ACTION_VERBS)
#: A negation immediately governing one of those verbs ("不要换", "别改").
#: Checked first and stripped, so a refusal is never read as a request.
_NEGATED_ACTION_RE = re.compile(rf"(?:不要|不用|不想|不需要|别|无需|不必)\s*(?:{_ACTION_VERBS})")


class TravelAIService:
    def __init__(
        self,
        trip_service: TripService,
        tool_registry: ToolRegistry,
        uow_factory,
        adapter: AIProvider | None = None,
        adapter_factory: Callable[[str | None], AIProvider | None] | None = None,
        max_tool_calls: int = 20,
        max_same_tool_calls: int = 5,
        recent_message_limit: int = 12,
    ) -> None:
        self.trip_service = trip_service
        self.tool_registry = tool_registry
        self.uow_factory = uow_factory
        self.adapter = adapter
        #: Resolves the model per request, because with per-owner credentials the
        #: model belongs to the caller rather than to the process. ``adapter``
        #: remains the fallback for tests and single-model deployments.
        self.adapter_factory = adapter_factory
        self.requirements = RequirementAgent(adapter=adapter)
        self.context_builder = ConversationContextBuilder(recent_message_limit)
        self.orchestrator = ToolOrchestrator(
            tool_registry, max_tool_calls=max_tool_calls, max_same_tool_calls=max_same_tool_calls
        )

    def _resolve_adapter(self, owner_id: str | None) -> tuple[AIProvider | None, str | None]:
        """The model for this caller, plus a warning when one is broken.

        "No model configured" returns ``(None, None)`` and is not a warning: the
        local deterministic planner takes over and the trip is still planned.
        A *configured but unusable* model returns a warning that reaches the user,
        because the alternative — silently planning without the model they set up,
        or worse, borrowing the operator's key — hides a real problem.
        """
        if self.adapter_factory is None:
            return self.adapter, None
        try:
            return self.adapter_factory(owner_id), None
        except Exception as exc:  # noqa: BLE001 - surfaced as a warning, not a crash
            logger.warning("model resolution failed; using local planner", exc_info=True)
            return None, f"AI model unavailable; local planner used: {exc}"

    async def chat(self, request: AIChatRequest, *, owner_id: str | None = None) -> AIChatResult:
        started = perf_counter()
        adapter, adapter_warning = self._resolve_adapter(owner_id)
        original_trip = self._load_trip(request.trip_id, owner_id=owner_id)
        # A caller who named a specific trip must get an error if it is not
        # theirs. Treating that as "no trip" used to fall through to creating a
        # brand-new trip, so a wrong or foreign id produced a silent empty
        # itinerary instead of a 404. Only an absent trip_id means "start fresh".
        if request.trip_id is not None and original_trip is None:
            raise TripNotFoundError(str(request.trip_id))
        requirement = await self.requirements.understand(request.message, adapter=adapter)
        conversation_id = self._get_conversation_id(request.trip_id)
        previous_messages = self._load_messages(conversation_id)

        if original_trip is None and not requirement.is_ready_for_trip:
            result = self._clarification_result(requirement)
            self._record_unbound_run(request, result, started)
            return result

        if original_trip is not None:
            requirement.unresolved_questions = []

        trip = original_trip
        if trip is None:
            # Created trips inherit the caller's ownership; without this the new
            # trip would be unowned and therefore invisible to its own creator.
            trip = await self.trip_service.create(requirement.to_profile(), owner_id=owner_id)
            conversation_id = self._get_or_create_conversation(trip.id)

        self._append_message(
            conversation_id,
            ConversationMessage(role=ConversationRole.USER, content=request.message),
        )
        context = self.context_builder.build(trip, previous_messages)
        explicit_write = self._has_explicit_write_intent(request.message)
        orchestration = await self._execute(
            trip,
            requirement,
            request.message,
            context,
            explicit_write,
            original_trip is None,
            adapter=adapter,
        )
        if adapter_warning:
            # Recorded on the result rather than only in the log, so a user whose
            # saved key cannot be read finds out from the response instead of
            # wondering why their model is not being used.
            orchestration.warnings.append(adapter_warning)
        latest_trip = self.trip_service.get(trip.id, owner_id=owner_id)
        changed = latest_trip.current_version != trip.current_version
        result = self._build_result(
            latest_trip,
            request.message,
            requirement,
            orchestration,
            changed or original_trip is None,
        )
        self._persist_turn(conversation_id, latest_trip, orchestration, result, started)
        return result

    def conversation(
        self, trip_id: UUID, *, owner_id: str | None = None
    ) -> list[ConversationMessage]:
        # Verify ownership first: the conversation table is keyed by trip id
        # alone, so without this check any caller could read another owner's chat
        # history by guessing a trip id.
        self.trip_service.get(trip_id, owner_id=owner_id)
        conversation_id = self._get_conversation_id(trip_id)
        if conversation_id is None:
            return []
        return self._load_messages(conversation_id, limit=100)

    async def _execute(
        self,
        trip: Trip,
        requirement: TripRequirement,
        message: str,
        context: dict[str, Any],
        explicit_write: bool,
        initial_plan: bool,
        adapter: AIProvider | None = None,
    ) -> ToolOrchestrationResult:
        effective_adapter = adapter if adapter is not None else self.adapter
        if effective_adapter is not None:
            try:
                model_result = await self.orchestrator.run_model(
                    effective_adapter,
                    AIRequest(
                        system_instruction=POLICY,
                        user_input=message,
                        context=context,
                        tools=self.tool_registry.openai_definitions(),
                        max_tokens=1200,
                    ),
                    allow_write=explicit_write,
                )
                if model_result.tool_calls or model_result.final_text:
                    return model_result
            except Exception as exc:
                logger.warning("AI model orchestration failed; using local planner", exc_info=True)
                fallback = ToolOrchestrationResult()
                fallback.warnings.append(f"AI provider unavailable; local planner used: {exc}")
            else:
                fallback = ToolOrchestrationResult()
        else:
            fallback = ToolOrchestrationResult()

        if re.search(r"加.*火锅|增加.*餐厅", message):
            searched = await self.orchestrator.run_actions(
                [
                    (
                        "search_restaurants",
                        trip.profile.model_dump(mode="json", exclude_computed_fields=True),
                    )
                ]
            )
            selected = None
            if searched.tool_results:
                data = searched.tool_results[0].result.get("data", [])
                selected = next(
                    (
                        item
                        for item in data
                        if "火锅" in item.get("canonical_name", "")
                        or "火锅" in "".join(item.get("cuisine", []))
                    ),
                    None,
                )
            if selected and explicit_write:
                revised = await self.orchestrator.run_actions(
                    [
                        (
                            "revise_trip",
                            {
                                "trip_id": str(trip.id),
                                "action": "replace_restaurant",
                                "constraints": {"restaurant_id": selected["id"]},
                            },
                        )
                    ],
                    allow_write=True,
                )
                searched.tool_calls.extend(revised.tool_calls)
                searched.tool_results.extend(revised.tool_results)
                searched.warnings.extend(revised.warnings)
            return searched

        actions = self._deterministic_actions(trip, requirement, message, initial_plan)
        local = await self.orchestrator.run_actions(actions, allow_write=explicit_write)
        local.warnings = fallback.warnings + local.warnings
        return local

    def _deterministic_actions(
        self, trip: Trip, requirement: TripRequirement, message: str, initial_plan: bool = False
    ) -> list[tuple[str, dict[str, Any]]]:
        profile = trip.profile.model_dump(mode="json", exclude_computed_fields=True)
        if initial_plan:
            return [
                ("search_hotels", profile),
                ("search_restaurants", profile),
                ("search_attractions", profile),
                ("calculate_route", profile),
                ("get_budget", {"trip_id": str(trip.id)}),
            ]
        if re.search(r"刷新|实时价格|现在价格|过期", message):
            return [("refresh_prices", {"trip_id": str(trip.id)})]
        if re.search(r"换酒店|酒店太贵|便宜一点|降低酒店预算", message):
            max_price = self._number_after(message, 1500)
            actions = []
            if any(offer.source_reference.is_stale for offer in trip.preview.hotel_offers):
                actions.append(("refresh_prices", {"trip_id": str(trip.id)}))
            actions.append(
                (
                    "replace_hotel",
                    {
                        "trip_id": str(trip.id),
                        "action": "replace_hotel",
                        "constraints": {"max_price": max_price},
                    },
                )
            )
            return actions
        if re.search(r"预算", message) and requirement.budget is not None:
            constraints: dict[str, Any] = {
                "total_budget": str(requirement.budget),
                "hotel_budget": str(requirement.hotel_budget)
                if requirement.hotel_budget is not None
                else str(trip.profile.hotel_budget or 1300),
            }
            return [
                (
                    "revise_trip",
                    {
                        "trip_id": str(trip.id),
                        "action": "change_budget",
                        "constraints": constraints,
                    },
                )
            ]
        if re.search(r"不要太累|别太累|太赶|慢一点|少一点景点|第三天", message):
            return [
                (
                    "revise_trip",
                    {
                        "trip_id": str(trip.id),
                        "action": "change_pace",
                        "constraints": {"pace": "relaxed"},
                    },
                )
            ]
        if re.search(r"加.*火锅|增加.*餐厅|找.*火锅|火锅", message):
            return [("search_restaurants", profile)]
        if re.search(r"酒店选择|有哪些酒店|看看酒店|酒店", message):
            return [("search_hotels", profile)]
        if re.search(r"餐厅|美食|吃什么", message):
            return [("search_restaurants", profile)]
        if re.search(r"景点|怎么玩|行程", message):
            return [("search_attractions", profile), ("calculate_route", profile)]
        return [
            ("search_hotels", profile),
            ("search_restaurants", profile),
            ("search_attractions", profile),
            ("calculate_route", profile),
            ("get_budget", {"trip_id": str(trip.id)}),
        ]

    def _build_result(
        self,
        trip: Trip,
        message: str,
        requirement: TripRequirement,
        orchestration: ToolOrchestrationResult,
        trip_updated: bool,
    ) -> AIChatResult:
        source_references = self._unique_sources(trip)
        warnings = list(orchestration.warnings)
        warnings.extend(
            f"{health.provider}: {health.status}"
            for health in (trip.preview.provider_health if trip.preview else [])
            if health.status != "healthy"
        )
        if any(reference.is_stale for reference in source_references):
            warnings.append("部分来源数据已过期，实时价格需要刷新确认。")
        change = trip.change_history[-1] if trip.change_history and trip_updated else None
        if trip_updated:
            budget_total = trip.budget.total if trip.budget else "未知"
            budget_limit = trip.budget.budget_limit if trip.budget else None
            message_text = (
                f"已根据“{message}”更新旅行方案。当前版本 {trip.current_version}，"
                f"计划金额 ¥{budget_total}"
                + (f" / 预算 ¥{budget_limit}" if budget_limit is not None else "")
                + "。"
            )
        else:
            message_text = self._read_result_message(message, trip)
        selected_hotel_id = None
        if trip.preview and trip.selected_hotel_offer_id:
            selected_offer = next(
                (
                    item
                    for item in trip.preview.hotel_offers
                    if item.id == trip.selected_hotel_offer_id
                ),
                None,
            )
            selected_hotel_id = selected_offer.hotel_id if selected_offer else None
        model_text = orchestration.final_text
        if model_text:
            message_text = model_text
        return AIChatResult(
            message=message_text,
            intent=self._intent(message, trip_updated),
            trip_id=trip.id,
            trip_updated=trip_updated,
            version=trip.current_version,
            selected_hotel_id=selected_hotel_id,
            selected_restaurants=trip.selected_restaurant_ids,
            selected_attractions=trip.selected_attraction_ids,
            budget_summary=trip.budget.model_dump(mode="json") if trip.budget else {},
            itinerary_summary=trip.itinerary.model_dump(mode="json").get("days", []),
            source_references=source_references,
            warnings=warnings,
            citations=[reference.source_id for reference in source_references],
            tool_calls=[call.model_dump(mode="json") for call in orchestration.tool_calls],
            change=change,
            unresolved_questions=requirement.unresolved_questions,
        )

    @staticmethod
    def _clarification_result(requirement: TripRequirement) -> AIChatResult:
        return AIChatResult(
            message="我先理解了已有信息，但还需要补充："
            + "；".join(requirement.unresolved_questions),
            intent="clarify_requirements",
            warnings=["日期未完整提供，未创建 Trip，也未生成实时价格。"],
            unresolved_questions=requirement.unresolved_questions,
        )

    @staticmethod
    def _read_result_message(message: str, trip: Trip) -> str:
        if "酒店" in message and trip.preview:
            return f"当前有 {len(trip.preview.hotels)} 家去重酒店、{len(trip.preview.hotel_offers)} 条房价 Offer，可按价格、评分和行程适配继续比较。"
        if "餐厅" in message or "美食" in message:
            return f"当前已聚合 {len(trip.preview.restaurants) if trip.preview else 0} 家餐厅候选，金额和来源保留在候选卡片中。"
        return "我已读取当前 Trip 的候选、预算和行程数据；你可以继续指定酒店、预算或某一天的节奏。"

    @staticmethod
    def _intent(message: str, changed: bool) -> str:
        if changed:
            return "revise_trip"
        if "酒店" in message:
            return "search_hotels"
        if "餐厅" in message or "美食" in message or "火锅" in message:
            return "search_restaurants"
        return "inspect_trip"

    @staticmethod
    def _number_after(text: str, default: int) -> int:
        match = re.search(r"(\d{3,6})", text)
        return int(match.group(1)) if match else default

    @staticmethod
    def _has_explicit_write_intent(message: str) -> bool:
        """Whether the message asks for a change, ignoring negated requests.

        This is a heuristic fallback, not an authorization decision: with a model
        adapter configured, the gate is the model's structured tool choice. The
        deterministic path has to be conservative because the old pattern
        (``re.search`` over bare verbs) granted write access to *negated* asks —
        "不要换酒店" matches both 换 and 不要, so declining a change authorised
        one. Negated clauses are therefore removed before matching, and a message
        is only treated as a request for change if some action verb is left
        un-negated.
        """
        if _NEGATED_ACTION_RE.search(message):
            # Drop each "negation + verb" span, then see if anything is left.
            message = _NEGATED_ACTION_RE.sub(" ", message)
        return bool(_ACTION_RE.search(message))

    @staticmethod
    def _unique_sources(trip: Trip):
        references = trip.preview.source_references if trip.preview else []
        unique = {}
        for reference in references:
            unique[reference.source + ":" + reference.source_id] = reference
        return list(unique.values())

    def _load_trip(self, trip_id: UUID | None, *, owner_id: str | None = None) -> Trip | None:
        """Load a trip the caller owns, or ``None``.

        Ownership is enforced by the service, so a trip belonging to somebody
        else is indistinguishable from a missing one here — which is what the
        404-on-foreign-id behaviour depends on.
        """
        if trip_id is None:
            return None
        try:
            return self.trip_service.get(trip_id, owner_id=owner_id)
        except TripNotFoundError:
            return None

    def _get_conversation_id(self, trip_id: UUID | None) -> UUID | None:
        if trip_id is None:
            return None
        with self.uow_factory() as uow:
            return uow.conversations.get_by_trip(trip_id)

    def _get_or_create_conversation(self, trip_id: UUID) -> UUID:
        with self.uow_factory() as uow:
            return uow.conversations.get_or_create(trip_id)

    def _load_messages(self, conversation_id: UUID | None, limit: int = 20):
        if conversation_id is None:
            return []
        with self.uow_factory() as uow:
            return uow.conversations.list_messages(conversation_id, limit)

    def _append_message(self, conversation_id: UUID, message: ConversationMessage) -> None:
        with self.uow_factory() as uow:
            uow.conversations.append_message(conversation_id, message)

    def _persist_turn(
        self,
        conversation_id: UUID,
        trip: Trip,
        orchestration: ToolOrchestrationResult,
        result: AIChatResult,
        started: float,
    ) -> None:
        with self.uow_factory() as uow:
            turn_started_at = datetime.now(timezone.utc)
            for index, (call, tool_result) in enumerate(
                zip(orchestration.tool_calls, orchestration.tool_results, strict=True),
                start=1,
            ):
                uow.conversations.record_tool_call(
                    conversation_id=conversation_id,
                    trip_id=trip.id,
                    tool_call_id=call.id,
                    tool_name=call.name,
                    arguments=call.arguments,
                    result_summary=tool_result.result,
                    success=tool_result.error is None,
                    duration_ms=tool_result.result.get("_latency_ms"),
                )
                uow.conversations.append_message(
                    conversation_id,
                    ConversationMessage(
                        role=ConversationRole.TOOL,
                        content=tool_result.model_dump_json(),
                        tool_name=call.name,
                        tool_arguments=call.arguments,
                        tool_result_summary=tool_result.result,
                        created_at=turn_started_at + timedelta(microseconds=index),
                    ),
                )
            uow.conversations.append_message(
                conversation_id,
                ConversationMessage(
                    role=ConversationRole.ASSISTANT,
                    content=result.message,
                    model=orchestration.responses[-1].model
                    if orchestration.responses
                    else "deterministic-local",
                    latency_ms=(perf_counter() - started) * 1000,
                    created_at=turn_started_at + timedelta(microseconds=1000),
                ),
            )
            response = orchestration.responses[-1] if orchestration.responses else None
            uow.conversations.record_run(
                conversation_id=conversation_id,
                trip_id=trip.id,
                request_id=response.request_id if response else str(UUID(int=0)),
                model=response.model if response else "deterministic-local",
                duration_ms=(perf_counter() - started) * 1000,
                input_tokens=response.input_tokens if response else None,
                output_tokens=response.output_tokens if response else None,
                estimated_cost=response.estimated_cost if response else None,
                success=True,
            )

    def _record_unbound_run(
        self, request: AIChatRequest, result: AIChatResult, started: float
    ) -> None:
        del request, result, started
