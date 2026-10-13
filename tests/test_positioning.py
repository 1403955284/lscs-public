"""站位建议的规则（站位建议设计文档第 3 节）：角色、目标顺序、步和校验。"""

import pytest
from factory import make_state, minion
from helpers import FIXTURES, NEEDS_REAL_CARDS, REAL_CARDS

from lscs.advisor.models import Move, PositionAdvice
from lscs.advisor.positioning import (
    MAX_MOVES,
    combat_role,
    exclusion,
    is_cleave,
    moves_toward,
    plan_positions,
    step_texts,
    validate_position,
)
from lscs.cards.db import CardDB
from lscs.state.models import OpponentView

CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")
NAME = CARDS.name

SPAWN = "BG36_116"  # 孢林卵怪：亡语召唤触手
SKULL = "BG28_300"  # 无害的骨颅：亡语召唤骷髅
HERO = "OG_221"  # 无私的英雄：亡语给圣盾
GLIM = "BG29_888"  # 微光护卫者：进击
IRON = "BG34_405"  # 铁血防御机：复仇（3）
CLEAVER = "BG26_817"  # 刀剑收藏家：劈砍
WILDFIRE = "BGS_126"  # 野火元素：击杀后的溢出伤害，不是劈砍
STITCHED = "BG31_999"  # 缝合回收者：战斗开始时消灭本随从左边的随从
FACELESS = "BG36_318"  # 无面转化者：亡语给神明加属性（括号里写着"酒馆法术"）
VANILLA = ("BG36_099", "BG36_098", "BG36_113", "BGS_119", "BG23_009")  # 卡牌库里没有说明


def role(card_id):
    return combat_role(minion(1, card_id, 1, 1), CARDS.get(card_id))


@pytest.mark.parametrize(
    "card_id",
    [SPAWN, "BG36_116_G", "BG21_029", "BG21_029_G", HERO, "BG_FP1_024", "FP1_024", SKULL, FACELESS],
)
def test_combat_deathrattles_go_left(card_id):
    assert role(card_id) == "deathrattle"
    assert exclusion(CARDS.get(card_id)) is None


def test_economy_words_only_count_in_the_deathrattle_sentence():
    # 无面转化者"亡语：使你的神明获得+2/+1。（在本局对战中你每施放一个酒馆法术都会提升！）"：
    # 括号里的"酒馆"是成长说明，不是拿牌，和飘荡祭品一样是战斗型
    assert role(FACELESS) == "deathrattle"
    cards = CardDB(
        [
            {"id": "A", "name": "甲", "text": "<b>亡语：</b>召唤一个1/1。获取一张酒馆币。"},
            {"id": "B", "name": "乙", "text": "<b>亡语：</b>获取一张酒馆币。（每回合都会提升！）"},
        ]
    )
    assert combat_role(minion(1, "A", 1, 1), cards.get("A")) == "deathrattle"  # 只看第一句
    assert combat_role(minion(1, "B", 1, 1), cards.get("B")) is None
    assert exclusion(cards.get("B")) == "economy_deathrattle"


@pytest.mark.parametrize("card_id", ["BG36_103", "BG36_103_G", "BG21_017"])
def test_economy_deathrattles_are_left_alone(card_id):
    # 恩拉其工兵"战吼，亡语：获取一张供能腔"、海上走私贩"亡语：获取一张酒馆币"
    assert role(card_id) is None
    assert exclusion(CARDS.get(card_id)) == "economy_deathrattle"


def test_cards_that_act_on_their_neighbour_are_left_alone():
    # 缝合回收者"战斗开始时：消灭本随从左边的随从。亡语：……"：拖到最左边就没有可消灭的了
    assert role(STITCHED) is None
    assert exclusion(CARDS.get(STITCHED)) == "adjacency"
    cards = CardDB(
        [
            {"id": "R", "name": "右", "text": "<b>进击：</b>对本随从右边的随从施放法术。"},
            {"id": "L", "name": "最左", "text": "<b>亡语：</b>使你最左边的随从获得+1/+1。"},
            {"id": "V", "name": "复仇", "text": "<b>复仇（2）：</b>使本随从左边的随从获得圣盾。"},
        ]
    )
    assert exclusion(cards.get("R")) == "adjacency"
    assert exclusion(cards.get("V")) == "adjacency"  # 放最右边也一样会落空
    # "最左边的随从"说的是场上的位置，不是邻居
    assert combat_role(minion(1, "L", 1, 1), cards.get("L")) == "deathrattle"


def test_stitched_recycler_is_not_dragged():
    assert plan(state(V1, V2, STITCHED)).moves == ()


def test_rally_avenge_and_the_brood_mother():
    assert role(GLIM) == "rally"
    assert role("BG36_849") is None  # 英勇的巢母：战斗开始时立即发起攻击，位置无关
    assert exclusion(CARDS.get("BG36_849")) == "attacks_at_start"
    assert role(IRON) == "avenge"
    assert exclusion(CARDS.get(GLIM)) is exclusion(CARDS.get(IRON)) is None
    assert exclusion(CARDS.get(V1)) is exclusion(None) is None  # 白板、不认识的卡


def test_cleave_and_unknown_cards():
    assert role(CLEAVER) is None and is_cleave(CARDS.get(CLEAVER))
    assert role(WILDFIRE) is None and not is_cleave(CARDS.get(WILDFIRE))
    assert not is_cleave(None)
    assert combat_role(minion(1, "NOPE_1", 1, 1), CARDS.get("NOPE_1")) is None
    for card_id in VANILLA:
        assert role(card_id) is None


@NEEDS_REAL_CARDS
def test_every_deathrattle_and_rally_card_in_the_real_pool_is_accounted_for():
    """不钉总数（卡池随版本变）：每张亡语随从要么有角色，要么被经济型、邻居这两条规则排除；
    每张进击随从要么是进击，要么被一条有名字的规则排除（或者前面的规则先给了角色）。"""
    cards = CardDB.from_json_file(REAL_CARDS)
    pool = cards.pool_minions()
    deathrattles = [c for c in pool if "DEATHRATTLE" in c.mechanics or "亡语：" in c.text]
    rallies = [c for c in pool if "BACON_RALLY" in c.mechanics or "进击：" in c.text]
    assert deathrattles and rallies
    for c in deathrattles:
        role = role_of(c)
        assert role in ("avenge", "deathrattle") or (
            role is None and exclusion(c) in ("economy_deathrattle", "adjacency")
        ), (c.card_id, c.name, c.text)
    for c in rallies:
        role = role_of(c)
        assert role in ("rally", "avenge", "deathrattle") or (
            role is None and exclusion(c) is not None
        ), (c.card_id, c.name, c.text)
    by_name = {c.name: c for c in pool}
    assert role_of(by_name["致命打击者"]) == "avenge"  # 复仇和亡语：复仇先判断
    assert role_of(by_name["莱，至高守护者"]) is None  # "战吼，亡语，进击：随机获取……"
    assert exclusion(by_name["莱，至高守护者"]) == "economy_deathrattle"
    assert exclusion(by_name["缝合回收者"]) == "adjacency"
    assert exclusion(by_name["海床招募者"]) == "adjacency"  # "进击：对本随从右边的随从……"
    assert exclusion(by_name["英勇的巢母"]) == "attacks_at_start"
    assert role_of(by_name["无面转化者"]) == "deathrattle"
    assert role_of(by_name["黑曜石掠夺者"]) == "rally"  # "目标及一个相邻"说的是攻击目标
    assert sum(is_cleave(c) for c in pool) == 2


def role_of(info):
    return combat_role(minion(1, info.card_id, 1, 1), info)


# ---- 目标顺序和步 ----


def board(*cards, taunts=()):
    """按顺序摆在场上，实体编号 101 起；taunts 是带嘲讽标签的序号（0 起）。"""
    return [
        minion(101 + i, c, 2, 2, keywords=("TAUNT",) if i in taunts else ())
        for i, c in enumerate(cards)
    ]


def state(*cards, taunts=(), opponent=None, seen=True, alive=True, next_opponent=True):
    s = make_state(turn=5, gold=0, seq=40, board=board(*cards, taunts=taunts))
    if opponent is None:
        return s
    opp = OpponentView(
        player_id=7,
        hero_card_id="H",
        health=20 if alive else 0,
        armor=0,
        place=None,
        tavern_tier=4,
        last_board=tuple(minion(900 + i, c, 3, 3) for i, c in enumerate(opponent)) if seen else (),
        last_seen_turn=3 if seen else None,
    )
    nxt = 7 if next_opponent else None
    return s.model_copy(update={"opponents": {7: opp}, "next_opponent_id": nxt})


def plan(s):
    advice = plan_positions(s, CARDS, NAME)
    if advice is not None:
        assert validate_position(advice, advice.board) is None
    return advice


def steps(advice):
    return [(m.entity_id, m.index, m.before_id) for m in advice.moves]


V1, V2, V3, V4 = VANILLA[:4]


def test_one_minion_or_only_vanilla():
    assert plan(state(SPAWN)) is None
    assert plan(make_state(board=())) is None
    advice = plan(state(V1, V2, V3))
    assert advice.moves == () and advice.target == advice.board == (101, 102, 103)
    assert (advice.state_seq, advice.turn, advice.more, advice.intro) == (40, 5, 0, False)


def test_deathrattle_third_goes_to_the_far_left():
    advice = plan(state(V1, V2, SPAWN))
    assert steps(advice) == [(103, 0, None)]
    move = advice.moves[0]
    assert (move.role, move.short) == ("deathrattle", "亡语最早触发")
    assert move.reason == "亡语随从先出手、先倒下，亡语最早生效，后面的随从都能用上"
    assert advice.target == (103, 101, 102)


def test_two_deathrattles_the_second_goes_left_of_a_minion():
    advice = plan(state(V1, SPAWN, V2, V3, SKULL))
    # 第 2 个先拖到最左边；第 5 个拖到 V1 的左边（做完第一步后 V1 在第 2 个槽位）
    assert steps(advice) == [(102, 0, None), (105, 1, 101)]
    texts = step_texts(
        advice, {m.entity_id: m.card_id for m in board(V1, SPAWN, V2, V3, SKULL)}, NAME
    )
    assert [(t.number, t.where) for t in texts] == [(2, "拖到最左边"), (5, "拖到 蚀脑怪 的左边")]


def test_deathrattle_before_rally():
    advice = plan(state(GLIM, V1, SKULL))
    assert advice.target == (103, 101, 102)
    assert steps(advice) == [(103, 0, None)]  # 拖完后进击正好排第二
    advice = plan(state(V1, GLIM, SKULL))
    assert steps(advice) == [(103, 0, None), (102, 1, 101)]
    assert [m.role for m in advice.moves] == ["deathrattle", "rally"]
    assert advice.moves[1].short == "进击要先出手"


def test_avenge_goes_to_the_far_right():
    advice = plan(state(IRON, V1, V2))
    assert steps(advice) == [(101, 2, None)]
    assert advice.moves[0].short == "复仇放右边等触发"
    texts = step_texts(advice, {101: IRON, 102: V1, 103: V2}, NAME)
    assert texts[0].where == "拖到最右边" and texts[0].number == 1


def test_avenge_and_deathrattle_both_wrong_takes_two_steps_deathrattle_first():
    # 设计文档第 6.1 节的例子：无害的骨颅在第 4、铁血防御机在第 2
    advice = plan(state(V1, IRON, V2, SKULL, V3))
    assert steps(advice) == [(104, 0, None), (102, 4, None)]
    cards = {m.entity_id: m.card_id for m in board(V1, IRON, V2, SKULL, V3)}
    texts = step_texts(advice, cards, NAME)
    assert [(t.number, NAME(t.card_id), t.where) for t in texts] == [
        (4, "无害的骨颅", "拖到最左边"),
        (3, "铁血防御机", "拖到最右边"),  # 做完第一步后它从第 2 变成第 3
    ]


def test_two_avenges_the_second_goes_left_of_the_other():
    advice = plan(state(IRON, V1, IRON, V2))
    assert advice.target == (102, 104, 101, 103)
    assert steps(advice) == [(103, 3, None), (101, 2, 103)]


def test_three_deathrattles_on_the_right_show_two_steps_and_more():
    advice = plan(state(V1, V2, SPAWN, SKULL, HERO))
    assert steps(advice) == [(103, 0, None), (104, 1, 101)]
    assert advice.more == 1 and len(advice.moves) == MAX_MOVES


def test_lone_taunt_goes_left_when_the_next_opponent_cleaves():
    s = state(V1, SPAWN, V2, V3, V4, taunts=(3,), opponent=(V1, CLEAVER))
    advice = plan(s)
    assert advice.cleave == CLEAVER
    assert advice.target == (104, 102, 101, 103, 105)
    assert steps(advice) == [(104, 0, None), (102, 1, 101)]
    taunt = advice.moves[0]
    assert (taunt.role, taunt.short) == ("taunt_edge", "对面有劈砍，嘲讽靠边")
    assert taunt.reason == (
        "对面有 刀剑收藏家（打一个连带打旁边的），嘲讽会被它打：放到最边上只连累一个邻居"
    )


def test_lone_taunt_already_on_the_far_right_stays():
    # 最右边也只有一个邻居：已经在那儿就不用动
    advice = plan(state(V1, SPAWN, V2, V3, taunts=(3,), opponent=(CLEAVER,)))
    assert advice.target == (102, 101, 103, 104)
    assert steps(advice) == [(102, 0, None)]  # 只剩亡语那一步
    # 有复仇随从要最右边那个槽位时，嘲讽还是去最左边
    advice = plan(state(IRON, V1, V2, taunts=(2,), opponent=(CLEAVER,)))
    assert advice.target == (103, 102, 101)
    assert steps(advice) == [(103, 0, None), (101, 2, None)]
    assert [m.role for m in advice.moves] == ["taunt_edge", "avenge"]
    # 嘲讽本身是复仇随从、已经在最右边：不动
    advice = plan(state(V1, V2, IRON, taunts=(2,), opponent=(CLEAVER,)))
    assert advice.moves == ()


def test_taunt_deathrattle_takes_the_edge_role():
    # 蹒跚的食尸鬼是嘲讽亡语：对面有劈砍时按靠边算，不再按亡语排
    advice = plan(state(V1, "BG_FP1_024", V2, taunts=(1,), opponent=(CLEAVER,)))
    assert steps(advice) == [(102, 0, None)] and advice.moves[0].role == "taunt_edge"


def test_two_taunts_are_left_alone():
    advice = plan(state(V1, SPAWN, V2, V3, taunts=(2, 3), opponent=(CLEAVER,)))
    assert advice.cleave == CLEAVER
    assert steps(advice) == [(102, 0, None)]  # 只剩亡语那一步
    assert {m.role for m in advice.moves} == {"deathrattle"}


def test_a_ghost_with_a_cleaver_counts():
    advice = plan(state(V1, V2, V3, taunts=(1,), opponent=(CLEAVER,), alive=False))
    assert steps(advice) == [(102, 0, None)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"opponent": (CLEAVER,), "next_opponent": False},  # 不知道下一个对手
        {"opponent": (CLEAVER,), "seen": False},  # 还没见过阵容
        {"opponent": (WILDFIRE, V1)},  # 野火元素不算劈砍
    ],
)
def test_no_edge_without_a_known_cleaver(kwargs):
    advice = plan(state(V1, V2, V3, taunts=(1,), **kwargs))
    assert advice.moves == () and advice.cleave is None


def test_minions_whose_place_does_not_matter_never_move():
    # 经济型亡语、英勇的巢母、圣盾、剧毒、风怒、嘲讽本身都不产生步
    cards = ["BG36_103", "BG21_017", "BG36_849", V1, V2, V3]
    s = make_state(
        board=[
            minion(101 + i, c, 2, 2, keywords=k)
            for i, (c, k) in enumerate(
                zip(
                    cards,
                    [(), (), (), ("DIVINE_SHIELD",), ("POISONOUS", "WINDFURY"), ("TAUNT",)],
                    strict=True,
                )
            )
        ]
    )
    assert plan(s).moves == ()


def test_moves_toward_without_left_fixes_every_slot_from_the_left():
    assert moves_toward((1, 2, 3), (3, 1, 2)) == [(3, 0, None)]
    assert moves_toward((1, 2, 3), (2, 3, 1)) == [(2, 0, None), (3, 1, 1)]
    assert moves_toward((1, 2), (1, 2)) == []


# ---- 校验 ----


def move(entity_id, index, before_id=None, role="deathrattle"):
    return Move(
        entity_id=entity_id, index=index, before_id=before_id, role=role, reason="", short=""
    )


def advice_of(board_ids, target, moves):
    return PositionAdvice(
        state_seq=1, turn=1, board=tuple(board_ids), target=tuple(target), moves=tuple(moves)
    )


def test_validate_position():
    good = advice_of((1, 2, 3), (3, 1, 2), [move(3, 0)])
    assert validate_position(good, (1, 2, 3)) is None
    assert validate_position(good, (1, 3, 2)) == "场面变了"
    assert validate_position(advice_of((1, 2, 3), (3, 1, 4), [move(3, 0)]), (1, 2, 3)) == (
        "目标顺序不是场上随从的排列"
    )
    assert validate_position(advice_of((1, 2, 3), (1, 1, 2), []), (1, 2, 3)) == (
        "目标顺序不是场上随从的排列"
    )
    in_place = advice_of((1, 2, 3), (1, 2, 3), [move(1, 0)])
    assert validate_position(in_place, (1, 2, 3)) == "第 1 步：#1 已经在第 1 个，原地不动"
    many = advice_of((1, 2, 3, 4), (4, 3, 2, 1), [move(4, 0), move(3, 1, 1), move(2, 2, 1)])
    assert validate_position(many, (1, 2, 3, 4)) == "步数超过 2 步"
    wrong = advice_of((1, 2, 3), (3, 1, 2), [move(2, 0)])
    assert validate_position(wrong, (1, 2, 3)) == "照着做完后和目标顺序不一致"
    right = advice_of((1, 2, 3), (2, 3, 1), [move(1, 2)])  # 往右拖：后缀要一致
    assert validate_position(right, (1, 2, 3)) is None
    assert validate_position(advice_of((1, 2, 3), (2, 3, 1), [move(2, 2)]), (1, 2, 3)) == (
        "照着做完后和目标顺序不一致"
    )


def test_validate_position_checks_entities_slots_and_neighbours():
    base = (1, 2, 3)
    assert validate_position(advice_of(base, base, [move(9, 0)]), base) == "第 1 步：场上没有 #9"
    assert (
        validate_position(advice_of(base, base, [move(3, 5)]), base) == "第 1 步：没有第 6 个槽位"
    )
    assert validate_position(advice_of(base, (3, 1, 2), [move(3, 0, 9)]), base) == (
        "第 1 步：场上没有 #9"
    )
    assert validate_position(advice_of(base, (3, 1, 2), [move(3, 0, 1)]), base) == (
        "第 1 步：拖完后右边不是 #1"
    )
    four = (1, 2, 3, 4)
    ok = advice_of(four, (1, 4, 2, 3), [move(4, 1, 2)])
    assert validate_position(ok, four) is None
    assert validate_position(advice_of(four, (1, 4, 2, 3), [move(4, 1, 3)]), four) == (
        "第 1 步：拖完后右边不是 #3"
    )
