import random
from pathlib import Path

from factory import hand_minion, make_state, minion, power, shop_minion, shop_spell

from lscs.advisor.context import build_context
from lscs.advisor.rules import rule_plans
from lscs.advisor.rules_config import load_rules_config
from lscs.advisor.validate import validate_plan

CFG = load_rules_config(Path(__file__).resolve().parents[1] / "strategy" / "current" / "rules.toml")
NAMES = {"A": "小鬼", "B": "海盗", "C": "元素", "W": "弱鸡"}


def name(card_id):
    return NAMES.get(card_id, card_id)


def plans_for(**kwargs):
    return rule_plans(build_context(make_state(**kwargs), buy_cost=3), CFG, name)


def steps(plan):
    return [(a.type, a.entity_id) for a in plan.actions]


def test_triple_comes_first():
    plans = plans_for(
        turn=3,
        gold=5,
        tier=2,
        upgrade_cost=7,
        board=[minion(10, "A", 1, 1), minion(11, "A", 1, 1)],
        shop=[shop_minion(20, "B", 5, 5), shop_minion(21, "A", 1, 1)],
    )
    top = plans[0]
    assert top.summary == "三连优先"
    assert steps(top)[0] == ("buy", 21)
    assert top.actions[0].reason == "凑成三连：小鬼变金色，还能发现一张高一本的随从"


def test_upgrade_when_behind_curve():
    plans = plans_for(
        turn=2, gold=4, tier=1, upgrade_cost=4, health=20, shop=[shop_minion(20, "A", 1, 1)]
    )
    assert steps(plans[0]) == [("upgrade", None)]
    assert plans[0].actions[0].reason == "第 2 回合按节奏该升 2 本了"
    assert steps(plans[1]) == [("buy", 20), ("refresh", None)]


def test_greedy_upgrade_with_healthy_hero():
    plans = plans_for(
        turn=3, gold=6, tier=2, upgrade_cost=6, health=30, shop=[shop_minion(20, "A", 1, 1)]
    )
    assert steps(plans[0]) == [("upgrade", None)]
    assert plans[0].actions[0].reason == "血量健康，第 3 回合可以提前升 3 本"


def test_no_upgrade_when_on_curve():
    plans = plans_for(
        turn=3, gold=5, tier=2, upgrade_cost=6, health=20, shop=[shop_minion(20, "A", 1, 1)]
    )
    assert all(a.type != "upgrade" for p in plans for a in p.actions)


def test_low_health_buys_before_upgrading():
    plans = plans_for(
        turn=4, gold=7, tier=2, upgrade_cost=6, health=10, shop=[shop_minion(20, "B", 4, 4)]
    )
    assert plans[0].summary == "买最好的身材"
    assert steps(plans[1])[0] == ("upgrade", None)


def test_buys_best_stats_then_refreshes_leftover():
    plans = plans_for(
        turn=3,
        gold=5,
        tier=2,
        upgrade_cost=7,
        shop=[shop_minion(20, "A", 1, 1), shop_minion(21, "B", 4, 5, keywords=["TAUNT"])],
    )
    assert steps(plans[0]) == [("buy", 21), ("refresh", None)]
    assert plans[0].actions[0].reason == "商店里身材最好，有嘲讽"
    assert plans[0].actions[1].reason == "剩 2 金，刷新找更好的牌"


def test_main_tribe_and_pair_in_reason():
    plans = plans_for(
        turn=3,
        gold=3,
        tier=2,
        upgrade_cost=7,
        board=[minion(10, "B", 2, 2, races=["PIRATE"])],
        shop=[shop_minion(20, "B", 2, 2, races=["PIRATE"]), shop_minion(21, "C", 3, 2)],
    )
    assert steps(plans[0]) == [("buy", 20)]
    assert plans[0].actions[0].reason == "商店里身材最好，契合主力种族海盗，凑成对子"
    assert plans[0].direction == "海盗"


def test_full_board_sells_weakest_for_a_better_minion():
    board = [minion(10 + i, f"X{i}", 3, 3) for i in range(6)] + [minion(16, "W", 1, 1)]
    plans = plans_for(
        turn=6, gold=3, tier=3, upgrade_cost=9, board=board, shop=[shop_minion(20, "B", 6, 6)]
    )
    assert steps(plans[0]) == [("sell", 16), ("buy", 20), ("refresh", None)]
    assert plans[0].actions[0].reason == "卖掉最弱的 弱鸡（1/1）腾位置"


def test_full_board_keeps_board_if_shop_is_worse():
    board = [minion(10 + i, f"X{i}", 3, 3) for i in range(7)]
    plans = plans_for(
        turn=6, gold=3, tier=3, upgrade_cost=9, board=board, shop=[shop_minion(20, "B", 1, 1)]
    )
    assert steps(plans[0]) == [("refresh", None)]
    assert plans[0].summary == "刷新找牌"  # 没有买牌，不能说"买最好的身材"


def test_summary_without_buys_names_what_the_plan_does():
    board = [minion(10 + i, f"X{i}", 3, 3) for i in range(7)]
    plans = plans_for(
        turn=6,
        gold=3,
        tier=3,
        upgrade_cost=9,
        board=board,
        shop=[shop_minion(20, "B", 1, 1)],
        hero_power_state=power(),
    )
    assert steps(plans[0]) == [("hero_power", None), ("refresh", None)]
    assert plans[0].summary == "用掉免费的英雄技能，刷新找牌"


def test_zero_gold_needs_no_action():
    plans = plans_for(gold=0, shop=[shop_minion(20, "A", 1, 1)])
    assert len(plans) == 1
    assert plans[0].actions == ()
    assert plans[0].summary == "本回合不需要操作"


def test_rules_never_buy_spells():
    plans = plans_for(gold=3, shop=[shop_spell(30, "S", cost=1)])
    assert all(a.type != "buy" for p in plans for a in p.actions)


def test_random_states_always_produce_valid_plans():
    rng = random.Random(20261011)
    for _ in range(300):
        board = [
            minion(100 + i, rng.choice("ABCDE"), rng.randint(1, 9), rng.randint(1, 9))
            for i in range(rng.randint(0, 7))
        ]
        hand = [hand_minion(200 + i, rng.choice("ABCDE"), 2, 2) for i in range(rng.randint(0, 2))]
        shop = [
            shop_minion(300 + i, rng.choice("ABCDE"), rng.randint(1, 9), rng.randint(1, 9))
            for i in range(rng.randint(0, 6))
        ]
        if rng.random() < 0.5:
            shop.append(shop_spell(399, "S", cost=rng.randint(0, 3)))
        state = make_state(
            turn=rng.randint(1, 12),
            gold=rng.randint(0, 10),
            tier=rng.randint(1, 6),
            health=rng.randint(1, 40),
            upgrade_cost=rng.choice([None, rng.randint(0, 11)]),
            refresh_cost=rng.choice([None, 1, 2]),
            board=board,
            hand=hand,
            shop=shop,
        )
        ctx = build_context(state, buy_cost=3)
        plans = rule_plans(ctx, CFG, name)
        assert plans, state
        for plan in plans:
            assert validate_plan(plan.actions, ctx) is None, (plan, state)


FREE = "免费技能，每回合都用掉"
AFTER_BUYING = "免费技能，买的随从打出去后再用"


def test_free_hero_power_after_buying_and_before_refreshing():
    plans = plans_for(
        turn=3,
        gold=5,
        tier=2,
        upgrade_cost=7,
        shop=[shop_minion(20, "A", 1, 1), shop_minion(21, "B", 4, 5, keywords=["TAUNT"])],
        hero_power_state=power(),
    )
    assert steps(plans[0]) == [("buy", 21), ("hero_power", None), ("refresh", None)]
    # 买来的随从在手牌里，要先打出去，加属性的技能才会作用到它
    assert plans[0].actions[1].reason == AFTER_BUYING


def test_free_hero_power_in_every_plan():
    plans = plans_for(
        turn=2,
        gold=4,
        tier=1,
        upgrade_cost=4,
        health=20,
        shop=[shop_minion(20, "A", 1, 1)],
        hero_power_state=power(),
    )
    assert steps(plans[0]) == [("upgrade", None), ("hero_power", None)]
    assert plans[0].actions[1].reason == FREE
    assert steps(plans[1]) == [("buy", 20), ("hero_power", None), ("refresh", None)]


def test_free_hero_power_even_without_gold():
    plans = plans_for(gold=0, shop=[shop_minion(20, "A", 1, 1)], hero_power_state=power())
    assert steps(plans[0]) == [("hero_power", None)]
    assert plans[0].summary == "用掉免费的英雄技能"


def test_rules_leave_other_hero_powers_to_the_ai():
    for hp in [
        power(cost=1),
        power(targets=(10,)),
        power(used=True),
        power(option="unusable", reason="REQ_NOT_EXHAUSTED_HERO_POWER"),
        power(option="absent"),
        power(option="unknown"),
        None,
    ]:
        plans = plans_for(
            gold=3,
            board=[minion(10, "B", 2, 2)],
            shop=[shop_minion(20, "A", 1, 1)],
            hero_power_state=hp,
        )
        assert all(a.type != "hero_power" for p in plans for a in p.actions), hp


def test_random_states_with_hero_powers_always_produce_valid_plans():
    rng = random.Random(20261012)
    for _ in range(300):
        board = [
            minion(100 + i, rng.choice("ABCDE"), rng.randint(1, 9), rng.randint(1, 9))
            for i in range(rng.randint(0, 7))
        ]
        shop = [
            shop_minion(300 + i, rng.choice("ABCDE"), rng.randint(1, 9), rng.randint(1, 9))
            for i in range(rng.randint(0, 6))
        ]
        option = rng.choice(["usable", "unusable", "absent", "unknown"])
        reason = rng.choice(["REQ_ENOUGH_MANA", "REQ_NOT_EXHAUSTED_HERO_POWER", "127"])
        targets = rng.choice([(), tuple(m.entity_id for m in board[:2])])
        hp = power(
            cost=rng.choice([0, 0, 1, 2]),
            option=option,
            used=rng.random() < 0.2,
            reason=reason if option == "unusable" else None,
            targets=targets,
        )
        state = make_state(
            turn=rng.randint(1, 12),
            gold=rng.randint(0, 10),
            tier=rng.randint(1, 6),
            health=rng.randint(1, 40),
            upgrade_cost=rng.choice([None, rng.randint(0, 11)]),
            refresh_cost=rng.choice([None, 1, 2]),
            board=board,
            shop=shop,
            hero_power_state=rng.choice([hp, hp, None]),
        )
        ctx = build_context(state, buy_cost=3)
        plans = rule_plans(ctx, CFG, name)
        assert plans, state
        for plan in plans:
            assert validate_plan(plan.actions, ctx) is None, (plan, state)


def test_hero_power_reminds_to_play_minions_already_in_hand():
    # 这回合早些时候买的随从还在手牌里：同样提醒先打出去
    plans = plans_for(
        turn=4,
        gold=0,
        hand=[hand_minion(30, "A", 1, 1)],
        shop=[shop_minion(20, "B", 2, 2)],
        hero_power_state=power(),
    )
    assert steps(plans[0]) == [("hero_power", None)]
    assert plans[0].actions[0].reason == AFTER_BUYING
