"""提醒和关键时刻（对局复盘设计文档第 4 节）。"""

from dataclasses import replace

import pytest
from factory import minion
from helpers import CC0_POOL, NEEDS_USER_LOG, REAL_LOG, USER_LOG, replay_log

from lscs.advisor.rules_config import RulesConfig
from lscs.review.flags import (
    GENERIC_FOCUS,
    Flag,
    find_flags,
    pick_moments,
    rules_focus,
)
from lscs.review.record import (
    ChoicePick,
    CombatRecord,
    GameRecord,
    RecordBuilder,
    TurnAdvice,
    TurnRecord,
)

RULES = RulesConfig(
    curve={2: 2, 4: 3, 6: 4, 8: 5, 10: 6},
    greedy_at=30,
    safe_below=15,
    buy_cost_default=3,
    keyword_score=2,
    tribe_score=2,
    spell_score=1,
)
NAMES = {"A": "蚀脑怪", "B": "虚悯者", "X": "飘荡祭品", "Y": "泰瑞尔"}


def name(card_id):
    return NAMES.get(card_id, card_id)


def board(count, races=("ABERRATION",)):
    return tuple(minion(100 + i, "A", 2, 2, races=races) for i in range(count))


def turn(number, **kw):
    """默认：1 本、没有操作、场上随从按基准、血量健康。"""
    tier = kw.pop("tier", 1)
    values = dict(
        turn=number,
        tier_start=tier,
        tier_end=tier,
        gold_start=3,
        gold_end=0,
        gold_gained=0,
        buys=(),
        spells=(),
        sells=(),
        plays=(),
        rerolls=0,
        reroll_gold=0,
        froze=False,
        hero_power=0,
        hero_power_gold=0,
        triples=(),
        choices=(),
        board_start=(),
        board_end=board(min(number, 7)),
        hand_end=0,
        health_start=30,
    )
    values.update(kw)
    return TurnRecord(**values)


def combat(number, lost=0, dealt=0, after=30, ghost=False):
    result = "win" if dealt else "loss" if lost else "ghost" if ghost else "tie"
    return CombatRecord(
        turn=number,
        opponent_id=2,
        opponent_hero="H",
        opponent_place=3,
        opponent_tier=3,
        opponent_minions=3,
        opponent_stats=30,
        my_minions=3,
        my_stats=30,
        lost=lost,
        dealt=dealt,
        ghost=ghost,
        result=result,
        health_after=after,
    )


def record(turns, combats=(), tribes=("ABERRATION", "DEMON"), hero="H"):
    return GameRecord(
        hero_card_id=hero,
        place=4,
        tribes=tribes,
        turns=tuple(turns),
        combats=tuple(combats),
        final_health=10,
        uncertain=False,
        seen_cards=frozenset(),
    )


def curve_turns(overrides=None):
    """10 个回合，按新手节奏升本（第 2、4、6、8、10 回合），每回合都买一张。"""
    tiers = {1: 1, 2: 2, 3: 2, 4: 3, 5: 3, 6: 4, 7: 4, 8: 5, 9: 5, 10: 6}
    turns = []
    for n in range(1, 11):
        start = tiers.get(n - 1, 1)
        values = dict(tier_start=start, tier_end=tiers[n], buys=("A",))
        values.update((overrides or {}).get(n, {}))
        turns.append(turn(n, **values))
    return turns


def kinds(flags):
    return [(f.kind, f.turns, f.severity) for f in flags]


def flags_of(turns, combats=(), **kw):
    return find_flags(record(turns, combats, **kw), RULES, name)


def test_a_game_on_curve_has_no_flags():
    assert [f for f in flags_of(curve_turns()) if f.kind != "good_turn"] == []


def test_tier_behind_by_one_and_by_two():
    turns = curve_turns({6: dict(tier_end=3), 7: dict(tier_start=3, tier_end=4)})
    behind = [f for f in flags_of(turns) if f.kind == "tier_behind"]
    assert kinds(behind) == [("tier_behind", (6,), 1)]
    flag = behind[0]
    assert flag.facts == "结束时还是 3 本，新手节奏这时该 4 本"
    assert flag.fix == "血量健康时按节奏升本：第 2、4、6、8、10 回合升到 2、3、4、5、6 本"
    turns = curve_turns({6: dict(tier_end=2)})
    assert ("tier_behind", (6,), 2) in kinds(flags_of(turns))


def test_tier_behind_at_two_checkpoints_merges_and_is_serious():
    turns = curve_turns(
        {
            6: dict(tier_end=3),
            7: dict(tier_start=3, tier_end=3),
            8: dict(tier_start=3, tier_end=4),
        }
    )
    flag = next(f for f in flags_of(turns) if f.kind == "tier_behind")
    assert (flag.turns, flag.severity) == ((6, 8), 2)
    assert flag.facts == "结束时分别是 3 本、4 本，新手节奏这时该 4 本、5 本"


def test_tier_behind_not_reported_when_health_is_low_or_turn_is_partial():
    low = curve_turns({6: dict(tier_end=3, health_start=15)})
    assert "tier_behind" not in [f.kind for f in flags_of(low)]
    partial = curve_turns({6: dict(tier_end=3, partial=True)})
    assert "tier_behind" not in [f.kind for f in flags_of(partial)]


def test_upgrade_without_board():
    turns = curve_turns({4: dict(buys=(), gold_start=7, upgrade_gold=4, board_end=board(2))})
    flag = next(f for f in flags_of(turns) if f.kind == "upgrade_without_board")
    # 只比基准少 1 个、血量健康：一般
    assert (flag.turns, flag.severity) == ((4,), 1)
    assert flag.facts == "升 3 本花光 7 金，没买随从，场上只有 2 个（这时该有 3 个）"
    # 升本后还剩 3 金（够买一张）：下次升本后接着买
    assert flag.fix.startswith("升本的回合剩下的铸币也要买一张随从；")
    # 少 2 个、或者血量不健康：要紧
    thin = curve_turns({5: dict(buys=(), tier_start=3, tier_end=4, board_end=board(2))})
    assert ("upgrade_without_board", (5,), 2) in kinds(flags_of(thin))
    hurt = curve_turns({4: dict(buys=(), health_start=15, board_end=board(2))})
    assert ("upgrade_without_board", (4,), 2) in kinds(flags_of(hurt))
    # 场上够了、或者买了随从，都不算
    enough = curve_turns({4: dict(buys=(), board_end=board(3))})
    assert "upgrade_without_board" not in [f.kind for f in flags_of(enough)]
    bought = curve_turns({4: dict(board_end=board(2))})
    assert "upgrade_without_board" not in [f.kind for f in flags_of(bought)]
    # 第 2 回合没有基准
    early = curve_turns({2: dict(buys=(), board_end=())})
    assert "upgrade_without_board" not in [f.kind for f in flags_of(early)]


def test_upgrade_without_board_then_a_big_hit_is_very_serious():
    turns = curve_turns({4: dict(buys=(), gold_end=2, board_end=board(2))})
    flag = next(
        f
        for f in flags_of(turns, [combat(5, lost=10, after=20)])
        if f.kind == "upgrade_without_board"
    )
    assert flag.severity == 3
    assert flag.facts.startswith("升 3 本剩 2 金，没买随从")


def test_upgrade_that_spends_all_the_gold_says_board_first():
    # 升本花光了铸币（剩的不够买一张）："剩下的铸币也要买随从"说不通，改成先补场面
    turns = curve_turns({4: dict(buys=(), gold_start=7, upgrade_gold=7, board_end=board(2))})
    flag = next(f for f in flags_of(turns) if f.kind == "upgrade_without_board")
    assert flag.fix == "场上比基准少时先补场面、下回合再升本"
    assert rules_focus((flag,)) == "场上比基准少时先补场面、下回合再升本"


def test_consecutive_upgrades_merge_and_absorb_the_hp_streak():
    turns = curve_turns(
        {
            7: dict(tier_start=4, tier_end=5, buys=(), board_end=board(5)),
            8: dict(tier_start=5, tier_end=6, buys=(), board_end=board(5)),
            9: dict(tier_start=6, tier_end=6),
            10: dict(tier_start=6, tier_end=6),
        }
    )
    combats = [combat(8, lost=15, after=19), combat(9, lost=15, after=4)]
    flags = flags_of(turns, combats)
    assert [f.kind for f in flags if f.severity] == ["upgrade_without_board"]
    flag = next(f for f in flags if f.kind == "upgrade_without_board")
    assert (flag.turns, flag.severity, flag.streak_turns, flag.streak_lost) == (
        (7, 8),
        3,
        (8, 9),
        30,
    )
    assert flag.facts == (
        "连续升本（5 本、6 本）但没买随从，场上一直只有 5 个（这时该有 7 个），"
        "接着第 8、9 回合连掉 30 血（34 → 4）"
    )
    assert flag.title == "第 7、8 回合 升本不补场面"


def test_hp_streak_rules():
    turns = curve_turns()
    three = [combat(3, lost=2, after=38), combat(4, lost=2, after=36), combat(5, lost=2, after=34)]
    flag = next(f for f in flags_of(turns, three) if f.kind == "hp_streak")
    assert (flag.turns, flag.severity, flag.facts) == ((3, 4, 5), 2, "连掉 6 血，只剩 34 血")
    two_small = [combat(3, lost=9, after=31), combat(4, lost=10, after=21)]
    assert "hp_streak" not in [f.kind for f in flags_of(turns, two_small)]
    two_big = [combat(3, lost=10, after=20), combat(4, lost=10, after=10)]
    flag = next(f for f in flags_of(turns, two_big) if f.kind == "hp_streak")
    assert (flag.turns, flag.severity) == ((3, 4), 3)  # 只剩 10 血
    broken = [combat(3, lost=10, after=20), combat(5, lost=10, after=10)]
    assert "hp_streak" not in [f.kind for f in flags_of(turns, broken)]
    out = [combat(9, lost=10, after=8), combat(10, lost=12, after=-4)]
    flag = next(f for f in flags_of(turns, out) if f.kind == "hp_streak")
    assert flag.facts == "连掉 22 血，被淘汰"


def test_reroll_heavy():
    turns = curve_turns({9: dict(rerolls=4, paid_rerolls=4, buys=("A",))})
    assert ("reroll_heavy", (9,), 1) in kinds(flags_of(turns))
    assert (
        next(f for f in flags_of(turns) if f.kind == "reroll_heavy").facts == "刷了 4 次只买了 1 张"
    )
    many = curve_turns({9: dict(rerolls=6, paid_rerolls=6, buys=())})
    assert ("reroll_heavy", (9,), 2) in kinds(flags_of(many))
    bought = curve_turns({9: dict(rerolls=8, paid_rerolls=8, buys=("A", "B"))})
    assert "reroll_heavy" not in [f.kind for f in flags_of(bought)]
    found = curve_turns({9: dict(rerolls=4, paid_rerolls=4, buys=("A",), triples=("A",))})
    assert "reroll_heavy" not in [f.kind for f in flags_of(found)]  # 刷到了三连
    found_late = curve_turns({9: dict(rerolls=6, paid_rerolls=6, buys=(), triples=("A",))})
    assert ("reroll_heavy", (9,), 2) in kinds(flags_of(found_late))  # 刷了 6 次才凑出来
    few = curve_turns({9: dict(rerolls=3, paid_rerolls=3, buys=())})
    assert "reroll_heavy" not in [f.kind for f in flags_of(few)]
    # 英雄技能、法术给的免费刷新不算
    free = curve_turns({9: dict(rerolls=6, paid_rerolls=2, buys=())})
    assert "reroll_heavy" not in [f.kind for f in flags_of(free)]


def test_gold_unspent_lists_all_turns_in_one_flag():
    one = curve_turns({6: dict(gold_end=3)})
    assert ("gold_unspent", (6,), 1) in kinds(flags_of(one))
    two = curve_turns({3: dict(gold_end=3), 9: dict(gold_end=4)})
    flag = next(f for f in flags_of(two) if f.kind == "gold_unspent")
    assert (flag.turns, flag.severity) == ((3, 9), 2)
    assert flag.facts == "分别剩了 3 金、4 金没用（共 7 金）"
    small = curve_turns({3: dict(gold_end=2), 9: dict(gold_end=1)})
    assert "gold_unspent" not in [f.kind for f in flags_of(small)]
    # 买牌价 2 的英雄剩 2 金也够买一张
    cheap = replace(RULES, buy_cost_heroes={"M": 2})
    flags = find_flags(record(small, hero="M"), cheap, name)
    assert ("gold_unspent", (3,), 1) in kinds(flags)


def test_no_direction():
    mixed = tuple(
        minion(200 + i, "A", 2, 2, races=(race,))
        for i, race in enumerate(
            ["ABERRATION", "ABERRATION", "DEMON", "DRAGON", "PIRATE", "MURLOC"]
        )
    )
    turns = curve_turns({7: dict(board_end=mixed), 8: dict(board_end=mixed)})
    tribes = ("ABERRATION", "DEMON", "DRAGON", "PIRATE", "MURLOC")
    flag = next(f for f in flags_of(turns, tribes=tribes) if f.kind == "no_direction")
    assert (flag.turns, flag.severity) == ((7, 8), 1)
    assert flag.facts == "场上 6 个随从，最多的畸变也只有 2 个"
    # 全种族的随从算进主方向
    amalgam = (*mixed[:5], minion(300, "Z", 2, 2, races=("ALL",)))
    turns = curve_turns({7: dict(board_end=amalgam)})
    assert "no_direction" not in [f.kind for f in flags_of(turns, tribes=tribes)]
    # 第 6 回合之前不看
    turns = curve_turns({6: dict(board_end=mixed)})
    assert "no_direction" not in [f.kind for f in flags_of(turns, tribes=tribes)]


def advice(**kw):
    values = dict(
        rules_upgrade=False,
        deep_summary="",
        deep_direction=None,
        deep_upgrade=False,
        advised=2,
        followed=1,
        missed_buys=(),
    )
    values.update(kw)
    return TurnAdvice(**values)


def test_advice_upgrade_ignored():
    turns = curve_turns({5: dict(advice=advice(rules_upgrade=True))})
    assert ("advice_upgrade_ignored", (5,), 1) in kinds(flags_of(turns))
    flag = next(f for f in flags_of(turns) if f.kind == "advice_upgrade_ignored")
    assert flag.facts == "建议升 4 本，你没升"
    # 之后的检查点落后了：要紧
    late = curve_turns({5: dict(advice=advice(fast_upgrade=True)), 6: dict(tier_end=3)})
    assert ("advice_upgrade_ignored", (5,), 2) in kinds(flags_of(late))
    # 血少不升本是对的；升了也不算
    low = curve_turns({5: dict(advice=advice(deep_upgrade=True), health_start=12)})
    assert "advice_upgrade_ignored" not in [f.kind for f in flags_of(low)]
    done = curve_turns({4: dict(advice=advice(rules_upgrade=True))})
    assert "advice_upgrade_ignored" not in [f.kind for f in flags_of(done)]


def test_advice_buys_ignored_needs_three_cards():
    two = curve_turns(
        {3: dict(advice=advice(missed_buys=("A",))), 6: dict(advice=advice(missed_buys=("X",)))}
    )
    assert "advice_buys_ignored" not in [f.kind for f in flags_of(two)]
    three = curve_turns(
        {
            3: dict(advice=advice(missed_buys=("A",))),
            6: dict(advice=advice(missed_buys=("X", "A"))),
        }
    )
    flag = next(f for f in flags_of(three) if f.kind == "advice_buys_ignored")
    assert (flag.turns, flag.severity) == ((3, 6), 1)
    assert flag.facts == "建议买的 3 张牌没买（蚀脑怪、飘荡祭品）"


def test_choice_disagree():
    pick = ChoicePick(id=1, kind="dark_gift", card_id="Y", cost=None, ai_card_id="X")
    same = ChoicePick(id=2, kind="dark_gift", card_id="Y", cost=None, ai_card_id="Y")
    turns = curve_turns({9: dict(choices=(pick, same))})
    flag = next(f for f in flags_of(turns) if f.kind == "choice_disagree")
    assert (flag.turns, flag.severity) == ((9,), 0)
    assert flag.facts == "1 次选项没按 AI 选（AI 推荐 飘荡祭品，你选了 泰瑞尔）"
    many = curve_turns({n: dict(choices=(pick,)) for n in (3, 6, 9)})
    assert ("choice_disagree", (3, 6, 9), 1) in kinds(flags_of(many))


def test_good_turns():
    turns = curve_turns({9: dict(triples=("A",)), 10: dict(buys=())})
    flags = [f for f in flags_of(turns, [combat(9, dealt=10)]) if f.kind == "good_turn"]
    # 升本当回合还买了随从的：第 2、4、6、8、10 回合（第 10 回合没买）
    assert [f.turns for f in flags] == [(2,), (4,), (6,), (8,), (9,)]
    assert flags[-1].facts == "凑出金色蚀脑怪，打出 10 血"
    assert flags[0].facts == "升 2 本的同时还买了 1 张随从"
    assert all(f.severity == 0 and f.fix == "" for f in flags)


def flag(kind, turns, severity, streak=()):
    fix = "" if kind == "good_turn" else f"{kind}；后半句"
    return Flag(kind=kind, turns=turns, severity=severity, facts="", fix=fix, streak_turns=streak)


def test_pick_moments_orders_by_severity_then_turn_and_skips_overlaps():
    flags = [
        flag("gold_unspent", (3,), 1),
        flag("upgrade_without_board", (7, 8), 3, streak=(8, 9)),
        flag("hp_streak", (9, 10), 2),  # 和上一条的第 9 回合重了
        flag("reroll_heavy", (11,), 2),
        flag("tier_behind", (4,), 1),
        flag("good_turn", (12,), 0),
    ]
    moments = pick_moments(flags, 13)
    assert [m.kind for m in moments] == ["upgrade_without_board", "reroll_heavy", "gold_unspent"]
    assert rules_focus(moments) == "upgrade_without_board"


def test_a_clean_win_leads_with_the_best_good_turn():
    flags = [
        flag("tier_behind", (4,), 1),
        Flag(kind="good_turn", turns=(3,), severity=0, facts="", fix="", weight=1),
        Flag(kind="good_turn", turns=(8,), severity=0, facts="", fix="", weight=3),
        Flag(kind="good_turn", turns=(11,), severity=0, facts="", fix="", weight=3),
    ]
    assert [m.turns for m in pick_moments(flags, 16)] == [(4,), (3,), (8,)]
    assert [m.turns for m in pick_moments(flags, 16, winner=True)] == [(8,), (4,), (3,)]


def test_tier_behind_and_ignored_upgrade_advice_on_adjacent_turns_pick_one():
    flags = [
        flag("advice_upgrade_ignored", (5,), 2),
        flag("tier_behind", (6,), 1),
        flag("gold_unspent", (9,), 1),
        flag("advice_upgrade_ignored", (12,), 1),
    ]
    # 第 5 回合没按建议升本、第 6 回合结束升本偏晚：说的是一件事，留严重度高的
    assert [m.turns for m in pick_moments(flags, 13)] == [(5,), (9,), (12,)]


def test_pick_moments_fills_with_good_turns_and_short_games_get_one():
    flags = [
        flag("good_turn", (11,), 0),
        flag("upgrade_without_board", (5,), 2),
        flag("good_turn", (5,), 0),
        flag("good_turn", (13,), 0),
    ]
    assert [m.turns for m in pick_moments(flags, 13)] == [(5,), (11,), (13,)]
    assert [m.turns for m in pick_moments(flags, 4)] == [(5,)]
    assert pick_moments([flag("choice_disagree", (3,), 0)], 13) == ()
    only_good = pick_moments([flag("good_turn", (2,), 0)], 13)
    assert rules_focus(only_good) == GENERIC_FOCUS
    assert rules_focus(()) == GENERIC_FOCUS


# ---- 真实日志 ----


def real_record(path, pool, rules):
    builder = RecordBuilder(rules.buy_cost_for)
    for state, events in replay_log(path, pool):
        builder.on_update(state, events)
    return builder.finish()


@pytest.fixture(scope="module")
def strategy_rules():
    from lscs.advisor.rules_config import load_rules_config
    from lscs.cli import DEFAULT_RULES

    return load_rules_config(DEFAULT_RULES)


@NEEDS_USER_LOG
def test_user_game_flags_and_moments(strategy_rules):
    from lscs.cli import DEFAULT_POOL
    from lscs.state.pool import load_pool_config

    names = {"BG36_099": "蚀脑怪", "BG36_103": "恩拉其工兵"}
    rec = real_record(USER_LOG, load_pool_config(DEFAULT_POOL), strategy_rules)
    flags = find_flags(rec, strategy_rules, lambda c: names.get(c, c))
    assert kinds(flags) == [
        ("upgrade_without_board", (5,), 1),  # 只比基准少 1 个、血量 36
        ("upgrade_without_board", (7, 8), 3),
        ("good_turn", (11,), 0),
        ("good_turn", (12,), 0),
        ("good_turn", (13,), 0),
    ]
    assert flags[1].facts.endswith("接着第 8、9 回合连掉 30 血（34 → 4）")
    assert flags[2].facts == "凑出金色蚀脑怪，打出 10 血"
    assert flags[4].facts == "凑出金色恩拉其工兵"
    moments = pick_moments(flags, len(rec.turns))
    assert [m.turns for m in moments] == [(7, 8), (5,), (11,)]
    # 第 5、7、8 回合升本都花光了铸币：下次先补场面
    assert flags[0].fix == flags[1].fix == "场上比基准少时先补场面、下回合再升本"
    assert rules_focus(moments) == "场上比基准少时先补场面、下回合再升本"


def test_cc0_game_flags(strategy_rules):
    rec = real_record(REAL_LOG, CC0_POOL, strategy_rules)
    flags = find_flags(rec, strategy_rules, name)
    # 设计文档的手算漏了这一条：第 4 回合结束还是 2 本（第 5 回合才升 3 本），按规则是升本偏晚
    assert [(f.kind, f.turns, f.severity) for f in flags if f.severity] == [
        ("tier_behind", (4,), 1)
    ]
    # 第 10 回合刷了 10 次但买了 9 张：不算刷新太多
    assert "reroll_heavy" not in [f.kind for f in flags]
    good = [f.turns[0] for f in flags if f.kind == "good_turn"]
    assert good == [3, 5, 6, 8, 9, 11, 12, 13]
    # 第 1 名、一滴血没掉：先写最好的回合（第 8 回合：三连、打出 12 血、升 5 本还买了 5 张）
    lost = sum(c.lost for c in rec.combats)
    moments = pick_moments(flags, rec.last_turn, winner=rec.place == 1 and lost == 0)
    assert [(m.kind, m.turns) for m in moments] == [
        ("good_turn", (8,)),
        ("tier_behind", (4,)),
        ("good_turn", (3,)),
    ]
