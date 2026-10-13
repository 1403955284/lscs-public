"""对局记录：从状态快照的差异提取操作和战斗结果（对局复盘设计文档第 3、5 节）。"""

import pytest
from factory import hand_minion, hand_spell, make_state, minion, power, shop_minion, shop_spell
from helpers import CC0_POOL, NEEDS_USER_LOG, REAL_LOG, USER_LOG, replay_log

from lscs.advisor.models import Action, Advice, Plan
from lscs.review.record import AdviceLog, ChoicePick, Op, RecordBuilder, attach_advice, turn_advice
from lscs.state.models import (
    Choice,
    ChoiceKind,
    ChoiceOption,
    Event,
    EventType,
    OpponentView,
    Phase,
)


def ev(event_type, turn=1, **data):
    return Event(type=event_type, turn=turn, data=data)


START = EventType.RECRUIT_STARTED


class Game:
    """按顺序喂快照：seq 自动递增。"""

    def __init__(self, buy_cost=3, normal_id=None):
        self.builder = RecordBuilder(buy_cost, normal_id)
        self.seq = 0
        self.builder.on_update(
            make_state(phase=Phase.HERO_SELECT, turn=0, seq=0), [ev(EventType.GAME_STARTED, 0)]
        )

    def feed(self, *events, **kw):
        self.seq += 1
        state = make_state(seq=self.seq, **kw)
        self.builder.on_update(state, list(events))
        return state

    def end(self, place=4, health=30, **kw):
        self.seq += 1
        state = make_state(seq=self.seq, phase=Phase.GAME_OVER, health=health, **kw)
        state = state.model_copy(update={"final_place": place})
        self.builder.on_update(state, [ev(EventType.GAME_ENDED, kw.get("turn", 1), place=place)])
        record = self.builder.finish()
        assert record is not None
        return record

    def turn(self, number=1):
        return self.end().turn(number)


def test_buy_to_hand_and_play():
    g = Game()
    shop = [shop_minion(10, "A", 2, 2), shop_minion(11, "B", 3, 3)]
    g.feed(ev(START), gold=3, shop=shop)
    g.feed(gold=0, shop=shop[1:], hand=[hand_minion(10, "A", 2, 2)])
    g.feed(gold=0, shop=shop[1:], board=[minion(10, "A", 2, 2)])
    t = g.turn()
    assert (t.buys, t.spells, t.plays, t.sells, t.triples) == (("A",), (), ("A",), (), ())
    assert (t.gold_start, t.gold_end, t.gold_gained) == (3, 0, 0)
    assert [m.entity_id for m in t.board_end] == [10]


def test_buy_spell_goes_to_spells():
    g = Game()
    shop = [shop_minion(10, "A", 2, 2), shop_spell(12, "S", 1)]
    g.feed(ev(START), gold=3, shop=shop)
    g.feed(gold=2, shop=shop[:1], hand=[hand_spell(12, "S")])
    g.feed(gold=2, shop=shop[:1])  # 打出法术：手牌里的法术消失，不是任何操作
    t = g.turn()
    assert (t.buys, t.spells, t.plays) == ((), ("S",), ())


def test_buying_the_third_copy_merges_in_hand():
    g = Game()
    shop = [shop_minion(10, "A", 2, 2), shop_minion(11, "B", 3, 3)]
    pair = [hand_minion(20, "A", 2, 2), hand_minion(21, "A", 2, 2)]
    g.feed(ev(START), gold=5, shop=shop, hand=pair)
    # 商店少一张、铸币 −3、手牌里那两张变成一张金色（新实体）
    g.feed(gold=2, shop=shop[1:], hand=[hand_minion(30, "A_G", 4, 4, golden=True)])
    t = g.turn()
    assert (t.buys, t.triples, t.sells) == (("A",), ("A",), ())


def test_vanished_shop_minion_without_enough_gold_spent_is_not_a_buy():
    g = Game()
    shop = [shop_minion(10, "A", 2, 2), shop_minion(11, "B", 3, 3)]
    g.feed(ev(START), gold=5, shop=shop)
    g.feed(gold=4, shop=shop[1:])  # 被什么效果拿走了，只花了 1 金
    assert g.turn().buys == ()


def test_playing_the_third_copy_merges_on_board_and_is_not_a_sell():
    g = Game()
    board = [minion(20, "A", 2, 2), minion(21, "A", 2, 2)]
    g.feed(ev(START), gold=0, board=board, hand=[hand_minion(22, "A", 2, 2)])
    g.feed(gold=0, board=[minion(30, "A_G", 4, 4, golden=True)])
    t = g.turn()
    assert (t.triples, t.sells, t.plays) == (("A",), (), ())


def test_merge_in_the_same_update_as_a_sell_is_not_counted_as_sells():
    g = Game()
    board = [minion(20, "A", 2, 2), minion(21, "A", 2, 2), minion(25, "C", 1, 1)]
    g.feed(ev(START), gold=0, board=board, hand=[hand_minion(22, "A", 2, 2)])
    # 同一次更新：卖掉 C（+1 金），打出第三张 A 合成金色
    g.feed(gold=1, board=[minion(30, "A_G", 4, 4, golden=True)])
    t = g.turn()
    assert (t.triples, t.sells) == (("A",), ("C",))


def test_golden_first_then_copies_vanish_next_update_is_not_a_sell():
    g = Game()
    board = [minion(20, "A", 2, 2), minion(21, "A", 2, 2), minion(25, "C", 1, 1)]
    g.feed(ev(START), gold=0, board=board, hand=[hand_minion(22, "A", 2, 2)])
    g.feed(gold=0, board=[*board, minion(30, "A_G", 4, 4, golden=True)])
    g.feed(gold=1, board=[minion(30, "A_G", 4, 4, golden=True)])  # 两张 A 和 C 同时消失，+1 金
    t = g.turn()
    assert (t.triples, t.sells) == (("A",), ("C",))


def test_copies_vanish_first_then_golden_next_update_is_not_a_sell():
    g = Game()
    board = [minion(20, "A", 2, 2), minion(21, "A", 2, 2), minion(25, "C", 1, 1)]
    g.feed(ev(START), gold=0, board=board, hand=[hand_minion(22, "A", 2, 2)])
    g.feed(gold=1, board=[])  # 两张 A 和 C 消失，+1 金（卖了 C）
    g.feed(gold=1, board=[minion(30, "A_G", 4, 4, golden=True)])
    t = g.turn()
    assert (t.triples, t.sells) == (("A",), ("C",))


def test_sell_needs_gold_to_go_up():
    g = Game()
    board = [minion(20, "A", 2, 2), minion(21, "B", 2, 2)]
    g.feed(ev(START), gold=0, board=board)
    g.feed(gold=1, board=board[1:])  # 卖
    g.feed(gold=1, board=[])  # 被战吼吃掉：铸币没变，不算
    t = g.turn()
    assert (t.sells, t.gold_gained, t.gold_end) == (("A",), 1, 1)


def test_hand_minion_that_disappears_is_nothing():
    g = Game()
    g.feed(ev(START), gold=0, hand=[hand_minion(20, "A", 2, 2)])
    g.feed(gold=0)
    t = g.turn()
    assert (t.plays, t.sells, t.buys) == ((), (), ())


def test_refresh_when_shop_changes_completely_or_refills_after_buying_out():
    g = Game()
    first = [shop_minion(10, "A", 2, 2)]
    second = [shop_minion(11, "B", 2, 2), shop_minion(12, "C", 2, 2)]
    third = [shop_minion(13, "D", 2, 2)]
    g.feed(ev(START), gold=6, shop=first)
    g.feed(gold=5, shop=second)  # 刷新
    g.feed(gold=5, shop=[*second, shop_minion(14, "E", 1, 1)])  # 只多了一张：不是刷新
    g.feed(gold=2, shop=second, hand=[hand_minion(14, "E", 1, 1)])
    g.feed(gold=2, shop=[], hand=[hand_minion(14, "E", 1, 1)])  # 商店被拿空（不是买）
    g.feed(gold=2, shop=third, hand=[hand_minion(14, "E", 1, 1)])  # 空了之后又有：刷新（免费）
    t = g.turn()
    assert (t.rerolls, t.reroll_gold, t.buys) == (2, 1, ("E",))


def test_upgrade_freeze_and_unfreeze():
    g = Game()
    shop = [shop_minion(10, "A", 2, 2), shop_minion(11, "B", 2, 2)]
    frozen = [s.model_copy(update={"frozen": True}) for s in shop]
    g.feed(ev(START), gold=6, tier=1, upgrade_cost=5, shop=shop)
    g.feed(gold=1, tier=2, upgrade_cost=7, shop=shop)
    g.feed(gold=1, tier=2, shop=frozen)
    g.feed(gold=1, tier=2, shop=shop)  # 又解冻了：本回合冻结过照样算
    t = g.turn()
    assert (t.tier_start, t.tier_end, t.upgrade_gold, t.froze) == (1, 2, 5, True)
    assert t.upgraded


def test_hero_power_use_and_cost():
    g = Game()
    g.feed(ev(START), gold=3, hero_power_state=power(cost=1))
    g.feed(gold=3, hero_power_state=power(cost=1, option="unknown"))
    g.feed(gold=2, hero_power_state=power(cost=1, used=True))
    g.feed(
        gold=2,
        hero_power_state=power(
            cost=1, used=True, option="unusable", reason="REQ_NOT_EXHAUSTED_HERO_POWER"
        ),
    )
    t = g.turn()
    assert (t.hero_power, t.hero_power_gold) == (1, 1)


def test_partial_turn_without_recruit_started():
    g = Game()
    g.feed(gold=3, main_action=False)  # 招募开始之前：不算
    g.feed(gold=3, shop=[shop_minion(10, "A", 2, 2)])  # 中途启动：第一个 main_action 的快照
    g.feed(gold=0, hand=[hand_minion(10, "A", 2, 2)])
    t = g.turn()
    assert (t.partial, t.gold_start, t.buys) == (True, 3, ("A",))


def test_choices_are_recorded_with_their_turn_and_price():
    g = Game()
    options = (
        ChoiceOption(
            entity_id=40,
            card_id="T1",
            card_type="BATTLEGROUND_TRINKET",
            index=0,
            atk=None,
            health=None,
            tier=None,
            cost=4,
        ),
        ChoiceOption(
            entity_id=41,
            card_id="T2",
            card_type="BATTLEGROUND_TRINKET",
            index=1,
            atk=None,
            health=None,
            tier=None,
            cost=2,
        ),
    )
    choice = Choice(
        id=7, kind=ChoiceKind.TRINKET, source_card_id="BG30_Trinket_1st", options=options
    )
    g.seq += 1
    offered = make_state(seq=g.seq, turn=6, gold=8, main_action=False).model_copy(
        update={"choice": choice}
    )
    g.builder.on_update(offered, [ev(EventType.CHOICE_OFFERED, 6, id=7, kind="trinket", count=2)])
    g.feed(
        ev(EventType.CHOICE_RESOLVED, 6, id=7, picked=40, card="T1"),
        turn=6,
        gold=4,
        main_action=False,
    )
    g.feed(ev(START, 6), turn=6, gold=4)
    t = g.end(turn=6).turn(6)
    assert t.choices == (ChoicePick(id=7, kind="trinket", card_id="T1", cost=4),)
    assert t.gold_start == 4


def opponent(player_id, health, *, place=2, board=(), seen=None):
    return OpponentView(
        player_id=player_id,
        hero_card_id=f"H{player_id}",
        health=health,
        armor=0,
        place=place,
        tavern_tier=3,
        last_board=tuple(board),
        last_seen_turn=seen,
    )


def test_combat_result_is_settled_at_the_next_recruit_start():
    g = Game()
    board = [minion(20, "A", 2, 3)]
    g.feed(ev(START), gold=0, board=board, health=30)
    opps = {2: opponent(2, 40), 3: opponent(3, 0, place=8)}
    g.seq += 1
    combat = make_state(seq=g.seq, phase=Phase.COMBAT, board=board, health=30)
    g.builder.on_update(
        combat.model_copy(update={"opponents": opps}),
        [ev(EventType.COMBAT_STARTED, opponent=2, hero="H2")],
    )
    seen = {2: opponent(2, 40, board=[minion(50, "X", 4, 4)], seen=1), 3: opps[3]}
    g.seq += 1
    ended = make_state(seq=g.seq, phase=Phase.COMBAT, board=board, health=30)
    g.builder.on_update(
        ended.model_copy(update={"opponents": seen}), [ev(EventType.COMBAT_ENDED, opponent=2)]
    )
    # 排行榜上对手的血量到下一回合招募开始才更新
    later = {2: opponent(2, 33, board=[minion(50, "X", 4, 4)], seen=1), 3: opps[3]}
    g.seq += 1
    nxt = make_state(seq=g.seq, turn=2, gold=4, board=board, health=30)
    g.builder.on_update(nxt.model_copy(update={"opponents": later}), [ev(START, 2)])
    # 第 2 回合打残影，掉 5 血；对局结束时结算
    g.seq += 1
    combat2 = make_state(seq=g.seq, turn=2, phase=Phase.COMBAT, board=board, health=30)
    g.builder.on_update(
        combat2.model_copy(update={"opponents": later}),
        [ev(EventType.COMBAT_STARTED, 2, opponent=3, hero="H3")],
    )
    record = g.end(turn=2, health=25, place=2)
    first, second = record.combats
    assert (first.result, first.dealt, first.lost, first.ghost) == ("win", 7, 0, False)
    assert (first.opponent_minions, first.opponent_stats, first.my_minions, first.my_stats) == (
        1,
        8,
        1,
        5,
    )
    assert (first.opponent_hero, first.opponent_place) == ("H2", 2)
    assert (second.result, second.lost, second.ghost, second.health_after) == ("loss", 5, True, 25)
    assert record.final_health == 25 and record.place == 2


def test_new_game_clears_and_updates_after_the_end_are_ignored():
    g = Game()
    g.feed(ev(START), gold=3)
    record = g.end()
    g.feed(gold=9, turn=2)
    assert g.builder.finish() == record
    g.builder.on_update(
        make_state(phase=Phase.HERO_SELECT, turn=0, seq=99), [ev(EventType.GAME_STARTED, 0)]
    )
    assert g.builder.finish() is None


def test_no_game_gives_no_record():
    assert RecordBuilder().finish() is None


# ---- 和建议比较 ----


def plan(*actions):
    return Plan(source="rules", actions=tuple(Action(type=t, entity_id=e) for t, e in actions))


def test_advice_is_deduplicated_and_only_counts_after_its_snapshot():
    ops = [
        Op(3, 5, "hero_power"),
        Op(3, 8, "buy", 10),
        Op(3, 9, "refresh"),
        Op(3, 12, "upgrade"),
        Op(4, 20, "buy", 11),  # 别的回合
    ]
    log = AdviceLog(
        rules_start=(4, plan(("buy", 10), ("hero_power", None), ("refresh", None))),
        fast=[(6, plan(("buy", 10), ("buy", 11))), (10, plan(("refresh", None)))],
        deep=Advice(
            state_seq=4,
            turn=3,
            plan=plan(("upgrade", None)).model_copy(update={"source": "llm", "summary": "先升本"}),
            lane="deep",
        ),
    )
    advice = turn_advice(3, log, ops, {10: "A", 11: "B"})
    # buy 10、hero_power、refresh、buy 11、upgrade：5 个；refresh 在第 10 个快照之后没有再做，
    # 但规则在第 4 个快照就建议过，按最早的算
    assert (advice.advised, advice.followed) == (5, 4)
    assert advice.missed_buys == ("B",)
    assert (advice.rules_upgrade, advice.deep_upgrade, advice.deep_summary) == (
        False,
        True,
        "先升本",
    )
    assert advice.upgrade_advised


def test_expired_deep_analysis_is_not_counted():
    deep = Advice(
        state_seq=1, turn=1, plan=plan(("upgrade", None)), lane="deep", expired="refreshed"
    )
    advice = turn_advice(1, AdviceLog(deep=deep), [], {})
    assert (advice.advised, advice.deep_upgrade, advice.upgrade_advised) == (0, False, False)


def test_advice_before_the_operation_snapshot_only():
    # 操作在建议之前就做了（快速建议基于更早的局面）不算采纳
    log = AdviceLog(fast=[(10, plan(("upgrade", None)))])
    advice = turn_advice(1, log, [Op(1, 9, "upgrade")], {})
    assert (advice.advised, advice.followed, advice.fast_upgrade) == (1, 0, True)


def test_attach_advice_fills_turns_and_choices():
    g = Game()
    g.feed(ev(START), gold=3, shop=[shop_minion(10, "A", 2, 2)])
    g.feed(gold=0, hand=[hand_minion(10, "A", 2, 2)])
    record = g.end()
    record = record.model_copy(
        update={
            "turns": (
                record.turns[0].model_copy(
                    update={
                        "choices": (ChoicePick(id=3, kind="dark_gift", card_id="X", cost=None),)
                    }
                ),
            )
        }
    )
    logs = {1: AdviceLog(rules_start=(1, plan(("buy", 10))))}
    out = attach_advice(
        record, g.builder.ops, g.builder.entity_cards, logs, hero_ai_pick="H", ai_choices={3: "Y"}
    )
    assert out.hero_ai_pick == "H"
    assert (out.turns[0].advice.advised, out.turns[0].advice.followed) == (1, 1)
    assert out.turns[0].choices[0].ai_card_id == "Y"


# ---- 两份真实日志 ----


def build(path, pool, buy_cost=3):
    builder = RecordBuilder(buy_cost)
    for state, events in replay_log(path, pool):
        builder.on_update(state, events)
    record = builder.finish()
    assert record is not None
    return record


@pytest.fixture(scope="module")
def user():
    from lscs.cli import DEFAULT_POOL
    from lscs.state.pool import load_pool_config

    return build(USER_LOG, load_pool_config(DEFAULT_POOL))


# 第 2.1 节的真值：(买随从, 买法术, 卖, 刷新, 回合末等级, 冻结, 技能)
USER_TRUTH = [
    (1, 0, 0, 0, 1, False, 1),
    (0, 0, 0, 0, 2, False, 1),
    (1, 1, 0, 0, 2, False, 1),
    (0, 0, 0, 1, 3, False, 1),
    (0, 0, 0, 0, 4, False, 1),
    (1, 0, 0, 0, 4, False, 1),
    (0, 0, 0, 0, 5, False, 1),
    (0, 0, 0, 0, 6, False, 1),
    (1, 0, 0, 0, 6, False, 1),
    (1, 2, 0, 2, 6, True, 1),
    (1, 1, 0, 4, 6, False, 1),
    (1, 2, 3, 3, 6, False, 1),
    (2, 0, 3, 3, 6, False, 1),
]


@NEEDS_USER_LOG
def test_user_game_turns_match_the_block_start_truth(user):
    assert [t.turn for t in user.turns] == list(range(1, 14))
    got = [
        (len(t.buys), len(t.spells), len(t.sells), t.rerolls, t.tier_end, t.froze, t.hero_power)
        for t in user.turns
    ]
    assert got == USER_TRUTH
    assert not any(t.partial for t in user.turns)


@NEEDS_USER_LOG
def test_user_game_triples_gold_board_and_choices(user):
    assert {t.turn: t.triples for t in user.turns if t.triples} == {
        11: ("BG36_099",),  # 金色蚀脑怪：买第三张时在手牌里合成
        13: ("BG36_103",),  # 金色恩拉其工兵：从手牌打出第三张时在场上合成
    }
    assert [t.gold_end for t in user.turns] == [0, 0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0]
    assert [len(t.board_end) for t in user.turns] == [1, 1, 3, 3, 3, 5, 5, 5, 6, 7, 7, 7, 7]
    trinket = next(c for c in user.turn(6).choices if c.kind == "trinket")
    assert (trinket.card_id, trinket.cost) == ("BG36_MagicItem_402", 4)
    assert user.turn(6).gold_start == 4  # 饰品 4 金在招募开始前就扣了
    assert [t.health_start for t in user.turns] == [40, 38, 36, 36, 36, 36, 34, 34, 19, 4, 4, 4, 4]


@NEEDS_USER_LOG
def test_user_game_combats(user):
    assert "".join(c.result[0].upper() for c in user.combats) == "LLWGWLTLLGWWL"
    assert [c.lost for c in user.combats] == [2, 2, 0, 0, 0, 2, 0, 15, 15, 0, 0, 0, 18]
    dealt = {c.turn: c.dealt for c in user.combats if c.dealt}
    assert dealt == {3: 5, 5: 7, 11: 10, 12: 15}  # 第 5 回合的伤害到下一回合才更新
    t8 = user.combat(8)
    assert (t8.opponent_minions, t8.opponent_stats, t8.my_minions, t8.my_stats) == (7, 194, 5, 79)
    assert [c.ghost for c in user.combats].count(True) == 2
    assert (user.place, user.final_health, user.hero_card_id) == (3, -14, "BG21_HERO_000")
    assert not user.uncertain


@pytest.fixture(scope="module")
def cc0():
    from lscs.advisor.rules_config import load_rules_config
    from lscs.cli import DEFAULT_RULES

    return build(REAL_LOG, CC0_POOL, load_rules_config(DEFAULT_RULES).buy_cost_for)


def test_cc0_game(cc0):
    assert [t.turn for t in cc0.turns] == list(range(1, 17))
    rerolls = {t.turn: t.rerolls for t in cc0.turns if t.rerolls}
    assert rerolls == {7: 6, 8: 2, 10: 10, 11: 5, 12: 3, 13: 7, 14: 5, 15: 7, 16: 8}
    assert sum(rerolls.values()) == 53
    assert [t.turn for t in cc0.turns if t.froze] == [2, 4, 5, 10]
    assert [t.turn for t in cc0.turns if t.upgraded] == [3, 5, 6, 8, 9]
    assert sum(len(t.buys) for t in cc0.turns) == 93
    # 已知偏差：第 7 回合灵魂吞噬者吃掉粗鄙小鬼给了 3 金，看起来和卖一样（真值 10）
    assert len(cc0.turn(7).sells) == 11
    # 第 11 回合两张梦魇融合怪合成金色：铸币没变，不算卖
    assert len(cc0.turn(11).sells) == 18
    assert all(c.lost == 0 for c in cc0.combats)
    assert [c.turn for c in cc0.combats if c.ghost] == [15]
    assert cc0.place == 1
    # 招募中途新出现的金色随从：第 6、8、11 回合各一个，第 12 回合两个。设计文档说"三连 8 次"，
    # 多出来的不是招募中合成的：第 10 回合招募开始之前就出现的金色白赚赌徒（旧式 TB_BaconUps_127），
    # 以及好几张金色的法术牌（TB_BaconShop_HP_008a，不是随从）
    assert {t.turn: len(t.triples) for t in cc0.turns if t.triples} == {6: 1, 8: 1, 11: 1, 12: 2}
    # 米尔豪斯刷新 2 金（第 7 回合刷了 6 次、花 12 金）
    assert (cc0.turn(7).rerolls, cc0.turn(7).paid_rerolls, cc0.turn(7).reroll_gold) == (6, 6, 12)


def test_buying_the_third_copy_in_the_same_update_as_an_upgrade():
    # 同一次更新里升本（4 金）又买第三张直接合成（3 金）：扣掉升本的钱还剩 3 金，认得出这次买
    g = Game()
    shop = [shop_minion(10, "A", 2, 2), shop_minion(11, "B", 3, 3)]
    pair = [hand_minion(20, "A", 2, 2), hand_minion(21, "A", 2, 2)]
    g.feed(ev(START), gold=8, tier=1, upgrade_cost=4, shop=shop, hand=pair)
    g.feed(gold=1, tier=2, shop=shop[1:], hand=[hand_minion(30, "A_G", 4, 4, golden=True)])
    t = g.turn()
    assert (t.buys, t.triples, t.upgrade_gold) == (("A",), ("A",), 4)
    # 升本（4 金）和技能（1 金）的钱不能当成买牌：商店里那张是被别的效果拿走的
    g = Game()
    g.feed(ev(START), gold=8, tier=1, upgrade_cost=4, shop=shop, hero_power_state=power(cost=1))
    g.feed(gold=3, tier=2, shop=shop[1:], hero_power_state=power(cost=1, used=True))
    t = g.turn()
    assert (t.buys, t.upgrade_gold, t.hero_power_gold) == ((), 4, 1)


def test_free_refreshes_are_not_paid_rerolls():
    g = Game()
    first, second, third = (
        [shop_minion(10, "A", 2, 2)],
        [shop_minion(11, "B", 2, 2)],
        [shop_minion(12, "C", 2, 2)],
    )
    g.feed(ev(START), gold=5, shop=first)
    g.feed(gold=5, shop=second)  # 免费刷新（英雄技能、法术）
    g.feed(gold=4, shop=third)
    t = g.turn()
    assert (t.rerolls, t.paid_rerolls, t.reroll_gold) == (2, 1, 1)


def test_reconnect_mid_game_keeps_the_real_turn_count():
    from datetime import datetime

    from test_review_flags import RULES

    from lscs.review.ai import build_review_message
    from lscs.review.report import history_entry, markdown, review_game, terminal_lines

    g = Game()
    g.feed(turn=7, gold=9, tier=4, main_action=True)  # 重连：第 7 回合中途开始记录
    for turn in range(8, 14):
        g.feed(ev(START, turn), turn=turn, gold=10, tier=4)
    record = g.end(turn=13)
    assert (record.last_turn, record.recorded_from, len(record.turns)) == (13, 7, 7)
    review = review_game(record, RULES, str, datetime(2026, 10, 11, 23, 20))
    title = "  📝 复盘：TB_BaconShop_HERO_49 第 4 名（13 回合，从第 7 回合起记录）"
    assert terminal_lines(review)[0] == title
    text = markdown(review, str)
    assert " · 13 个回合（从第 7 回合起记录）\n" in text and "| 7（中途） |" in text
    assert history_entry(review)["turns"] == 13
    # 按打到第几回合算：不是只有 7 回合的短对局
    message = build_review_message(review.record, review.flags, review.moments, str)
    assert message.startswith(
        "对局：TB_BaconShop_HERO_49 | 第 4 名 | 13 个回合（从第 7 回合起记录）"
    )
