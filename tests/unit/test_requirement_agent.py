import asyncio

from travel_api.services.requirement_agent import RequirementAgent


def test_requirement_agent_extracts_explicit_and_inferred_fields():
    requirement = asyncio.run(
        RequirementAgent().understand(
            "去成都玩四天，两个人，预算5000，喜欢吃，不想太赶，2026年10月3日到2026年10月7日"
        )
    )

    assert requirement.destination == "成都"
    assert requirement.start_date.isoformat() == "2026-10-03"
    assert requirement.end_date.isoformat() == "2026-10-07"
    assert requirement.travelers == 2
    assert str(requirement.budget) == "5000"
    assert requirement.pace.value == "relaxed"
    assert requirement.field_origins["budget"].origin == "explicit"
    assert requirement.field_origins["travelers"].origin == "inferred"
    assert "hotel_breakfast" in requirement.unknown_fields
    assert requirement.unresolved_questions == []


def test_requirement_agent_does_not_invent_missing_dates():
    requirement = asyncio.run(RequirementAgent().understand("去成都玩四天，两个人，预算5000"))

    assert requirement.destination == "成都"
    assert requirement.start_date is None
    assert requirement.end_date is None
    assert not requirement.is_ready_for_trip
    assert any("日期" in question for question in requirement.unresolved_questions)
