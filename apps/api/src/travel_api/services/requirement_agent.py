from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from travel_api.domain.models import Pace, TripProfile
from travel_api.services.ai_contracts import AIProvider, AIRequest

logger = logging.getLogger(__name__)


class RequirementField(BaseModel):
    value: Any = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    origin: str = "explicit"


class TripRequirement(BaseModel):
    destination: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    travelers: int | None = None
    budget: Decimal | None = None
    hotel_budget: Decimal | None = None
    food_budget: Decimal | None = None
    transport_budget: Decimal | None = None
    travel_style: str | None = None
    pace: Pace | None = None
    hotel_preferences: list[str] = Field(default_factory=list)
    food_preferences: list[str] = Field(default_factory=list)
    attraction_preferences: list[str] = Field(default_factory=list)
    mobility_preferences: list[str] = Field(default_factory=list)
    must_visit: list[str] = Field(default_factory=list)
    must_avoid: list[str] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    field_origins: dict[str, RequirementField] = Field(default_factory=dict)
    raw_input: str = ""

    @property
    def is_ready_for_trip(self) -> bool:
        return bool(self.destination and self.start_date and self.end_date and self.travelers)

    def to_profile(self) -> TripProfile:
        if not self.is_ready_for_trip:
            raise ValueError("destination, dates and travelers are required")
        return TripProfile(
            destination=self.destination,
            start_date=self.start_date,
            end_date=self.end_date,
            travelers=self.travelers,
            total_budget=self.budget,
            hotel_budget=self.hotel_budget,
            food_budget=self.food_budget,
            transport_budget=self.transport_budget,
            pace=self.pace or Pace.BALANCED,
            hotel_preferences=self.hotel_preferences,
            food_preferences=self.food_preferences,
            attraction_preferences=self.attraction_preferences,
            mobility_constraints=self.mobility_preferences,
            must_visit=self.must_visit,
            must_avoid=self.must_avoid,
        )


class RequirementAgent:
    """Requirement understanding with deterministic fallback for offline demos."""

    date_pattern = re.compile(
        r"(?:(?P<year>20\d{2})\s*年\s*)?(?P<month>\d{1,2})\s*月\s*(?P<day>\d{1,2})\s*日?"
    )

    def __init__(self, adapter: AIProvider | None = None) -> None:
        self.adapter = adapter

    async def understand(self, text: str, *, adapter: AIProvider | None = None) -> TripRequirement:
        """Extract requirements, optionally with a caller-supplied model.

        ``adapter`` overrides the one passed to the constructor so a per-owner
        model can be used without rebuilding the agent. ``None`` means "use the
        constructor's", not "use no model", which is why this is a keyword-only
        override rather than a replacement.
        """
        effective = adapter if adapter is not None else self.adapter
        if effective is not None:
            try:
                response = await effective.complete(
                    AIRequest(
                        system_instruction=(
                            "Extract travel requirements as JSON. Do not invent dates, "
                            "prices, availability, or entities. Return null for unknown values."
                        ),
                        user_input=text,
                        response_format={"type": "json_object"},
                        max_tokens=800,
                    )
                )
                if response.text:
                    parsed = TripRequirement.model_validate_json(response.text)
                    parsed.raw_input = text
                    return self._add_missing_questions(parsed)
            except Exception as exc:
                # The deterministic parser is the offline and failure-safe path.
                logger.warning(
                    "AI requirement extraction failed; using local parser: %s", exc, exc_info=True
                )
        return self.parse(text)

    def parse(self, text: str) -> TripRequirement:
        result = TripRequirement(raw_input=text)
        self._parse_destination(result, text)
        self._parse_dates(result, text)
        self._parse_travelers(result, text)
        self._parse_budgets(result, text)
        self._parse_preferences(result, text)
        return self._add_missing_questions(result)

    @staticmethod
    def _set(
        result: TripRequirement,
        field: str,
        value: Any,
        confidence: float = 1.0,
        origin: str = "explicit",
    ) -> None:
        setattr(result, field, value)
        result.field_origins[field] = RequirementField(
            value=value, confidence=confidence, origin=origin
        )

    def _parse_destination(self, result: TripRequirement, text: str) -> None:
        match = re.search(
            r"(?:去|到|前往|目的地(?:是|：|:)?)\s*"
            r"(?P<destination>[\u4e00-\u9fff]{2,8}?)(?:玩|旅游|旅行|，|,|\s|$)",
            text,
        )
        if match:
            self._set(result, "destination", match.group("destination"))

    def _parse_dates(self, result: TripRequirement, text: str) -> None:
        matches = list(self.date_pattern.finditer(text))
        if len(matches) >= 2:
            current_year = date.today().year
            values = []
            for match in matches[:2]:
                year = int(match.group("year") or current_year)
                values.append(date(year, int(match.group("month")), int(match.group("day"))))
            self._set(
                result, "start_date", values[0], 0.95 if not matches[0].group("year") else 1.0
            )
            self._set(result, "end_date", values[1], 0.95 if not matches[1].group("year") else 1.0)
            return
        if len(matches) == 1:
            days_match = re.search(r"(\d+)\s*(?:天|日)", text)
            if days_match:
                match = matches[0]
                start = date(
                    int(match.group("year") or date.today().year),
                    int(match.group("month")),
                    int(match.group("day")),
                )
                self._set(result, "start_date", start, 0.9 if not match.group("year") else 1.0)
                self._set(
                    result,
                    "end_date",
                    start + timedelta(days=int(days_match.group(1))),
                    0.75,
                    "inferred",
                )

    def _parse_travelers(self, result: TripRequirement, text: str) -> None:
        match = re.search(r"(\d+)\s*(?:人|位|个人)", text)
        if match:
            self._set(result, "travelers", int(match.group(1)))
        elif re.search(r"女朋友|男朋友|情侣|夫妻|两个人|我们", text):
            self._set(result, "travelers", 2, 0.68, "inferred")

    def _parse_budgets(self, result: TripRequirement, text: str) -> None:
        for field, label in (
            ("hotel_budget", "酒店预算"),
            ("food_budget", "餐饮预算"),
            ("transport_budget", "交通预算"),
            ("budget", "预算"),
        ):
            match = re.search(rf"{label}[^0-9０-９]*(\d+(?:\.\d+)?)\s*([万千]?)", text)
            if match:
                multiplier = {"万": 10000, "千": 1000}.get(match.group(2), 1)
                self._set(result, field, Decimal(match.group(1)) * multiplier)

    def _parse_preferences(self, result: TripRequirement, text: str) -> None:
        if re.search(r"不想太赶|不要太赶|轻松|悠闲|慢节奏", text):
            self._set(result, "pace", Pace.RELAXED)
            self._set(result, "travel_style", "relaxed", 0.92)
        elif re.search(r"紧凑|多安排|特种兵", text):
            self._set(result, "pace", Pace.FULL)
            self._set(result, "travel_style", "full", 0.85)
        if re.search(r"美食|吃|火锅|小吃|餐厅", text):
            result.food_preferences.extend(["美食", "火锅/小吃"])
        if re.search(r"地铁|交通方便|出行方便", text):
            result.hotel_preferences.append("地铁附近")
            result.mobility_preferences.append("metro_access")
        if re.search(r"景点|打卡|博物馆", text):
            result.attraction_preferences.append("景点")

    @staticmethod
    def _add_missing_questions(result: TripRequirement) -> TripRequirement:
        questions = []
        if not result.destination:
            questions.append("你想去哪个目的地？")
        if not result.start_date or not result.end_date:
            questions.append("你还没有告诉我出发日期和结束日期。")
        if not result.travelers:
            questions.append("这次有几位旅行者？")
        result.unresolved_questions = questions
        if "hotel_breakfast" not in result.unknown_fields:
            result.unknown_fields.append("hotel_breakfast")
        return result
