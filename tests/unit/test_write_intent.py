"""Write-intent detection must not grant permission for a refused change.

The previous implementation searched for bare action verbs:

    re.search(r"换|改|降低|增加|删除|移除|调整|刷新|重新规划|不要", message)

"不要换酒店" ("don't change the hotel") contains both 换 and 不要, so a message
*declining* a change authorised one. Since this feeds ``allow_write`` on the AI
path, a negation bug is an authorization bug.
"""

from __future__ import annotations

import pytest
from travel_api.services.travel_ai import TravelAIService

detect = TravelAIService._has_explicit_write_intent


@pytest.mark.parametrize(
    "message",
    [
        "换一家酒店",
        "把酒店换成便宜点的",
        "降低预算",
        "增加一个餐厅",
        "删除第二天的景点",
        "移除这个景点",
        "调整一下行程顺序",
        "刷新一下价格",
        "重新规划第三天",
        "替换现在的酒店",
        "取消这个安排",
    ],
)
def test_clear_change_requests_are_detected(message):
    assert detect(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "不要换酒店",
        "不用改行程",
        "别调整顺序了",
        "不需要刷新价格",
        "无需增加餐厅",
        "不必删除景点",
        "我不想换酒店",
        "不要重新规划",
        "不要替换酒店",
        "别移除这个景点",
    ],
)
def test_negated_changes_are_not_write_requests(message):
    """The reported bug: a refusal must never authorise a write."""
    assert detect(message) is False, (
        f"{message!r} declines a change but was treated as a request for one"
    )


@pytest.mark.parametrize(
    "message",
    [
        "成都有什么好吃的",
        "帮我看看这个行程",
        "第二天天气怎么样",
        "有哪些景点值得去",
        "",
    ],
)
def test_read_only_questions_are_not_write_requests(message):
    assert detect(message) is False


def test_negation_of_one_verb_does_not_mask_a_real_request():
    """A refusal about one thing must not hide a request about another."""
    assert detect("不要换酒店，但是增加一个餐厅") is True
    assert detect("别改日期，帮我调整一下景点顺序") is True


def test_negation_pattern_matches_immediately_governing_word_only():
    """The negation must actually govern the verb, not merely appear nearby."""
    # "不要" applies to 辣, not to 换: this *is* a request to change.
    assert detect("不要辣的，换一家餐厅") is True
