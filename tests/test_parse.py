import json

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
            ' {"type": "refresh"}, {"type": "refresh"}, {"type": "refresh"}]}',
            "动作超过 5 个",
        ),
        ('{"actions": ["buy"]}', "第 1 个动作不是对象"),
        ('{"actions": [{"type": "discover"}]}', "第 1 个动作类型未知：discover"),
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


def test_hero_power_with_or_without_target():
    plan = parse_plan(
        '{"actions": [{"type": "buy", "entity_id": 50}, {"type": "buy", "entity_id": 51},'
        ' {"type": "hero_power", "reason": "买完再加属性"},'
        ' {"type": "hero_power", "entity_id": "#451"}, {"type": "freeze"}]}'
    )
    assert plan.actions == (
        Action(type="buy", entity_id=50),
        Action(type="buy", entity_id=51),
        Action(type="hero_power", reason="买完再加属性"),
        Action(type="hero_power", entity_id=451),
        Action(type="freeze"),
    )


def test_analysis_and_look_for():
    plan = parse_plan(
        '{"analysis": " 前期血厚，先升本。 ", "actions": [],'
        ' "look_for": [" 飘荡祭品 ", "神明", "", 3, null]}'
    )
    assert plan.analysis == "前期血厚，先升本。"
    assert plan.look_for == ("飘荡祭品", "神明")  # 空的和不是文字的跳过


def test_analysis_and_look_for_are_optional():
    plan = parse_plan('{"actions": []}')
    assert (plan.analysis, plan.look_for) == ("", ())
    plan = parse_plan('{"actions": [], "analysis": 3, "look_for": "神明"}')
    assert (plan.analysis, plan.look_for) == ("", ())  # 类型不对时忽略，不影响合法性


def test_look_for_is_limited():
    names = [f"牌{i}" for i in range(8)]
    long_name = "很" * 30
    plan = parse_plan(json.dumps({"actions": [], "look_for": [long_name, *names]}))
    assert len(plan.look_for) == 6  # 最多 6 个
    assert plan.look_for[0] == "很" * 19 + "…"  # 每个最长 20 字
    assert plan.look_for[1:] == tuple(names[:5])
