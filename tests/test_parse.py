import pytest

from lscs.advisor.models import Action
from lscs.llm.parse import EmptyResponse, ParseError, parse_plan


def test_valid_answer():
    plan = parse_plan(
        '{"actions": [{"type": "buy", "entity_id": 50, "reason": " 凑对子 "},'
        ' {"type": "upgrade", "reason": "节奏"}],'
        ' "summary": "先买后升", "direction": "海盗", "confidence": 0.8}'
    )
    assert plan.source == "llm"
    assert plan.actions == (
        Action(type="buy", entity_id=50, reason="凑对子"),
        Action(type="upgrade", reason="节奏"),
    )
    assert (plan.summary, plan.direction, plan.confidence) == ("先买后升", "海盗", 0.8)


def test_lenient_fields():
    plan = parse_plan(
        '{"actions": [{"type": "sell", "entity_id": "#31"}, {"type": "refresh", "entity_id": 9}],'
        ' "direction": "", "confidence": 3}'
    )
    assert plan.actions == (Action(type="sell", entity_id=31), Action(type="refresh"))
    assert (plan.summary, plan.direction, plan.confidence) == ("", None, 1.0)
    assert parse_plan('{"actions": [], "confidence": "高"}').confidence is None


def test_non_finite_confidence_is_ignored():
    assert parse_plan('{"actions": [], "confidence": NaN}').confidence is None
    assert parse_plan('{"actions": [], "confidence": 1' + "0" * 400 + "}").confidence is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("不是json", "不是合法的 JSON"),
        ("[1, 2]", "不是 JSON 对象"),
        ('{"summary": "x"}', "缺少 actions 列表"),
        (
            '{"actions": [{"type": "refresh"}, {"type": "refresh"}, {"type": "refresh"},'
            ' {"type": "refresh"}, {"type": "refresh"}]}',
            "动作超过 4 个",
        ),
        ('{"actions": ["buy"]}', "第 1 个动作不是对象"),
        ('{"actions": [{"type": "hero_power"}]}', "第 1 个动作类型未知：hero_power"),
        ('{"actions": [{"type": "buy"}]}', "第 1 个动作缺少 entity_id"),
        ('{"actions": [{"type": "sell", "entity_id": true}]}', "第 1 个动作缺少 entity_id"),
    ],
)
def test_invalid_answers(text, message):
    with pytest.raises(ParseError) as exc:
        parse_plan(text)
    assert str(exc.value) == message
    assert not isinstance(exc.value, EmptyResponse)


def test_empty_answer():
    with pytest.raises(EmptyResponse):
        parse_plan("  \n")


def test_running_totals_are_accepted_and_ignored():
    plan = parse_plan(
        '{"actions": [{"type": "buy", "entity_id": 50, "gold_after": 4, "minions_after": 5,'
        ' "reason": "凑对子"}, {"type": "upgrade", "gold_after": -1, "minions_after": 5}]}'
    )
    assert plan.actions == (
        Action(type="buy", entity_id=50, reason="凑对子"),
        Action(type="upgrade"),
    )
