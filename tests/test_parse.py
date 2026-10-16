import json

import pytest

from lscs.advisor.models import Action, HeroComment
from lscs.llm.parse import EmptyResponse, ParseError, parse_hero_pick, parse_plan


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
        # 站位只由规则提示，AI 没有 position 动作（站位建议设计文档第 7 节）
        ('{"actions": [{"type": "position", "order": [1, 2]}]}', "第 1 个动作类型未知：position"),
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


# ---- 选英雄 ----

OFFERED = (92, 89, 90, 91)
SIGNATURE = ((89, "BG21_HERO_000"), (90, "BG28_HERO_800"), (91, "BG33_HERO_001"), (92, "X"))


def hero_answer(**fields):
    return json.dumps({"pick": 89, "reason": "0 费技能白送属性", **fields}, ensure_ascii=False)


def test_hero_pick_valid():
    advice = parse_hero_pick(
        hero_answer(
            reason=" 0 费技能\n每回合白送属性 ",
            heroes=[
                {"entity_id": 90, "comment": " 要靠对子才强 "},
                {"entity_id": "#91", "comment": "技能复杂"},
            ],
            confidence=0.7,
        ),
        OFFERED,
        signature=SIGNATURE,
    )
    assert (advice.source, advice.pick, advice.signature) == ("llm", 89, SIGNATURE)
    assert advice.reason == "0 费技能 每回合白送属性"  # 合成一行
    assert advice.comments == (
        HeroComment(entity_id=90, text="要靠对子才强"),
        HeroComment(entity_id=91, text="技能复杂"),
    )
    assert (advice.confidence, advice.elapsed) == (0.7, None)


def test_hero_pick_accepts_hash_numbers():
    assert parse_hero_pick(hero_answer(pick="#92"), OFFERED).pick == 92


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("不是json", "不是合法的 JSON"),
        ("[89]", "不是 JSON 对象"),
        ('{"reason": "好"}', "缺少 pick（推荐的英雄编号）"),
        ('{"pick": "凯瑞尔"}', "缺少 pick（推荐的英雄编号）"),
        ('{"pick": true}', "缺少 pick（推荐的英雄编号）"),
        ('{"pick": 93}', "推荐的英雄不在备选里"),
    ],
)
def test_invalid_hero_picks(text, message):
    with pytest.raises(ParseError) as exc:
        parse_hero_pick(text, OFFERED)
    assert str(exc.value) == message
    assert not isinstance(exc.value, EmptyResponse)


def test_empty_hero_answer():
    with pytest.raises(EmptyResponse):
        parse_hero_pick(" ", OFFERED)


def test_hero_comments_skip_bad_items():
    heroes = [
        "不是对象",
        {"comment": "没有编号"},
        {"entity_id": 99, "comment": "不在备选里"},
        {"entity_id": 90, "comment": ""},
        {"entity_id": 90, "comment": 3},
        {"entity_id": 91, "comment": "第一条"},
        {"entity_id": 91, "comment": "重复的"},
        {"entity_id": 89, "comment": "推荐的也可以"},
    ]
    advice = parse_hero_pick(hero_answer(heroes=heroes), OFFERED)
    assert [(c.entity_id, c.text) for c in advice.comments] == [
        (91, "第一条"),
        (89, "推荐的也可以"),
    ]
    assert parse_hero_pick(hero_answer(heroes={"90": "x"}), OFFERED).comments == ()


def test_hero_reason_and_comments_are_cut():
    advice = parse_hero_pick(
        hero_answer(reason="理" * 61, heroes=[{"entity_id": 90, "comment": "评" * 21}]), OFFERED
    )
    assert advice.reason == "理" * 59 + "…"  # 最多 60 字
    assert advice.comments[0].text == "评" * 19 + "…"  # 最多 20 字
    fits = parse_hero_pick(hero_answer(reason="理" * 60), OFFERED)
    assert fits.reason == "理" * 60


def test_hero_reason_missing():
    for reason in ("", None, 3):
        advice = parse_hero_pick(json.dumps({"pick": 89, "reason": reason}), OFFERED)
        assert advice.reason == "（AI 没有给理由）"
    assert parse_hero_pick('{"pick": 89}', OFFERED).confidence is None


# ---- 选项 ----

CHOICE_IDS = [6931, 6932, 6933]


def test_choice_pick_is_parsed():
    from lscs.llm.parse import parse_choice_pick

    advice = parse_choice_pick(
        '{"pick": 6933, "reason": "32/32 直接成主力", "confidence": 0.8}', CHOICE_IDS, 12
    )
    assert (advice.choice_id, advice.pick, advice.reason, advice.confidence) == (
        12,
        6933,
        "32/32 直接成主力",
        0.8,
    )
    assert parse_choice_pick('{"pick": "#6931", "reason": "x"}', CHOICE_IDS).pick == 6931


def test_choice_pick_must_be_one_of_the_options():
    import pytest

    from lscs.llm.parse import ParseError, parse_choice_pick

    with pytest.raises(ParseError, match="推荐的选项不在选项里"):
        parse_choice_pick('{"pick": 1, "reason": "x"}', CHOICE_IDS)
    with pytest.raises(ParseError, match="pick"):
        parse_choice_pick('{"reason": "x"}', CHOICE_IDS)
    with pytest.raises(ParseError, match="pick"):
        parse_choice_pick('{"pick": true}', CHOICE_IDS)
    with pytest.raises(ParseError):
        parse_choice_pick("[6933]", CHOICE_IDS)


def test_choice_reason_is_one_cut_line():
    from lscs.llm.parse import CHOICE_REASON_CHARS, parse_choice_pick

    long = "很长" * 20
    advice = parse_choice_pick(f'{{"pick": 6931, "reason": "第一行\\n{long}"}}', CHOICE_IDS)
    assert advice.reason == ("第一行 " + long)[: CHOICE_REASON_CHARS - 1] + "…"
    assert CHOICE_REASON_CHARS == 30
    assert parse_choice_pick('{"pick": 6931}', CHOICE_IDS).reason == "（AI 没有给理由）"


def test_empty_choice_answer_can_be_retried():
    import pytest

    from lscs.llm.parse import EmptyResponse, parse_choice_pick

    with pytest.raises(EmptyResponse):
        parse_choice_pick("  ", CHOICE_IDS)


# ---- 整局计划（整局计划与流派指南设计文档第 4.3 节） ----

PLAN = {
    "comp": " 畸变神明 ",
    "tribe": "畸变",
    "key_cards": ["蚀脑怪", "残忍的精神鞭笞者(金)", "飘荡祭品"],
    "tiers": "第 6 回合 4 本，第 8 回合 5 本",
    "roll": "5 本后铸币主要刷新找核心卡；\n血量低于 15 先补身材",
    "stage": "过渡",
    "pivot": "第 8 回合还没有两张核心卡就转海盗",
    "changed": "",
}


def plan_answer(plan) -> str:
    return json.dumps({"actions": [], "plan": plan}, ensure_ascii=False)


def test_game_plan_fields_are_read():
    from lscs.advisor.models import GamePlan

    plan = parse_plan(plan_answer(PLAN))
    assert plan.game_plan == GamePlan(
        comp="畸变神明",
        tribe="ABERRATION",
        key_cards=("蚀脑怪", "残忍的精神鞭笞者", "飘荡祭品"),  # 去掉"(金)"
        tiers="第 6 回合 4 本，第 8 回合 5 本",
        roll="5 本后铸币主要刷新找核心卡； 血量低于 15 先补身材",  # 合成一行
        stage="过渡",
        pivot="第 8 回合还没有两张核心卡就转海盗",
    )
    assert plan.game_plan.turn == 0  # 回合由调度器校验时填


def test_game_plan_fields_are_cut_and_capped():
    raw = dict(
        PLAN,
        comp="一二三四五六七八九十甲乙丙",
        key_cards=["甲", 7, "", "乙", "甲", "丙", "丁"],
        tiers="长" * 50,
        changed=" 连输两场 ",
    )
    plan = parse_plan(plan_answer(raw)).game_plan
    assert plan.comp == "一二三四五六七八九十甲…"  # 12 个字
    assert plan.key_cards == ("甲", "乙", "丙")  # 跳过不是文字的、空的、重复的，最多 3 张
    assert plan.tiers == "长" * 39 + "…"
    assert plan.changed == "连输两场"


@pytest.mark.parametrize(
    ("tribe", "expected"),
    [("畸变", "ABERRATION"), ("PIRATE", "PIRATE"), ("pirate", "PIRATE"), ("火星人", None)],
)
def test_game_plan_tribe(tribe, expected):
    assert parse_plan(plan_answer(dict(PLAN, tribe=tribe))).game_plan.tribe == expected


def test_game_plan_tribe_all_or_missing_is_none():
    assert parse_plan(plan_answer(dict(PLAN, tribe="ALL"))).game_plan.tribe is None
    assert parse_plan(plan_answer(dict(PLAN, tribe=None))).game_plan.tribe is None


@pytest.mark.parametrize("stage", ["中期", "", 3, None])
def test_unknown_stage_is_none(stage):
    assert parse_plan(plan_answer(dict(PLAN, stage=stage))).game_plan.stage is None


@pytest.mark.parametrize("stage", ["前期", "过渡", "成型", "保血"])
def test_known_stages(stage):
    assert parse_plan(plan_answer(dict(PLAN, stage=stage))).game_plan.stage == stage


@pytest.mark.parametrize(
    "raw", [None, "畸变神明", ["畸变神明"], {}, {"comp": ""}, {"comp": 3}, {"key_cards": ["甲"]}]
)
def test_no_game_plan_is_not_an_error(raw):
    data = {"actions": [{"type": "refresh"}], "summary": "刷"}
    if raw is not None:
        data["plan"] = raw
    plan = parse_plan(json.dumps(data, ensure_ascii=False))
    assert plan.game_plan is None and plan.summary == "刷"
    assert plan.actions == (Action(type="refresh"),)


@pytest.mark.parametrize(
    ("comp", "expected"),
    [
        ("畸变怪养神流（亡语 / 弃牌）", "畸变怪养神流"),
        ("畸变神明（畸变）", "畸变神明"),
        ("守门融合怪喝茶流（中立法术流）", "守门融合怪喝茶流"),  # 不截成半个括号
        ("海盗(金币) (钩牙)", "海盗"),
        ("（未定）", "（未定）"),  # 去光了就保留原样
        ("圣盾龙（希望信使）流", "圣盾龙（希望信使）流"),  # 只去末尾的
    ],
)
def test_game_plan_comp_drops_trailing_notes_before_cutting(comp, expected):
    assert parse_plan(plan_answer(dict(PLAN, comp=comp))).game_plan.comp == expected


def test_long_comp_names_are_matched_before_cutting():
    # HSReplay 的英文名比 PLAN_COMP_CHARS 长：截断成"Aberrations…"就对不上别名了
    seen = []

    def canonical(name):
        seen.append(name)
        return "畸变·梦魇侵蚀怪" if name == "Aberrations - Sludge" else None

    answer = plan_answer(dict(PLAN, comp="Aberrations - Sludge（弃牌）"))
    assert parse_plan(answer, canonical).game_plan.comp == "畸变·梦魇侵蚀怪"
    assert seen == ["Aberrations - Sludge"]  # 去掉末尾的括号说明之后
    other = plan_answer(dict(PLAN, comp="Dragons - Spell Power"))
    assert parse_plan(other, canonical).game_plan.comp == "Dragons - S…"  # 对不上：照旧截断
    assert parse_plan(answer).game_plan.comp == "Aberrations…"


def test_placeholder_and_undecided_comps_are_not_matched():
    from lscs.llm.parse import PLAN_PLACEHOLDERS

    def canonical(name):
        raise AssertionError(f"不该对 {name}")

    assert parse_plan(plan_answer(dict(PLAN, comp="未定")), canonical).game_plan.comp == "未定"
    placeholder = plan_answer(dict(PLAN, comp=PLAN_PLACEHOLDERS["comp"]))
    assert parse_plan(placeholder, canonical).game_plan.comp == "未定"


def test_game_plan_key_cards_drop_the_display_marks():
    cards = ["蚀脑怪✓", "残忍的精神鞭笞者 ✔", "飘荡祭品(金)✓"]
    plan = parse_plan(plan_answer(dict(PLAN, key_cards=cards))).game_plan
    assert plan.key_cards == ("蚀脑怪", "残忍的精神鞭笞者", "飘荡祭品")


def test_a_copied_placeholder_plan_is_undecided_and_empty():
    from lscs.advisor.models import GamePlan
    from lscs.llm.parse import PLAN_PLACEHOLDERS

    plan = parse_plan(plan_answer(dict(PLAN_PLACEHOLDERS))).game_plan
    assert plan == GamePlan(comp="未定")  # 示例原样抄回来：什么都没定
    partly = dict(
        PLAN, comp="目标流派", key_cards=["核心卡名", "蚀脑怪"], pivot="换方向的条件一句话"
    )
    plan = parse_plan(plan_answer(partly)).game_plan
    assert (plan.comp, plan.key_cards, plan.pivot) == ("未定", ("蚀脑怪",), "")
    assert plan.tiers == PLAN["tiers"]  # 真写了的照旧
