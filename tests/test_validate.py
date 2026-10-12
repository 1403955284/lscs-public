from factory import hand_minion, make_state, minion, power, shop_minion, shop_spell

from lscs.advisor.context import build_context
from lscs.advisor.models import Action
from lscs.advisor.validate import validate_plan


def ctx_for(**kwargs):
    return build_context(make_state(**kwargs), buy_cost=3)


def buy(entity_id):
    return Action(type="buy", entity_id=entity_id)


def sell(entity_id):
    return Action(type="sell", entity_id=entity_id)


UPGRADE = Action(type="upgrade")
REFRESH = Action(type="refresh")
FREEZE = Action(type="freeze")
SHOP = [shop_minion(20, "A", 2, 2), shop_minion(21, "B", 3, 3), shop_minion(22, "C", 1, 4)]


def test_empty_plan_is_valid():
    assert validate_plan([], ctx_for()) is None


def test_buy_within_gold():
    assert validate_plan([buy(20)], ctx_for(gold=3, shop=SHOP)) is None


def test_buy_needs_gold():
    assert validate_plan([buy(20), buy(21)], ctx_for(gold=5, shop=SHOP)) == "第 2 步：铸币不够"


def test_buy_must_be_in_shop_and_only_once():
    assert validate_plan([buy(99)], ctx_for(gold=10, shop=SHOP)) == "第 1 步：商店里没有 #99"
    assert validate_plan([buy(20), buy(20)], ctx_for(gold=10, shop=SHOP)) == (
        "第 2 步：#20 已经买过"
    )


def test_selling_first_pays_for_a_buy():
    board = [minion(10, "X", 1, 1)]
    assert validate_plan([sell(10), buy(20)], ctx_for(gold=2, shop=SHOP, board=board)) is None
    assert validate_plan([buy(20), sell(10)], ctx_for(gold=2, shop=SHOP, board=board)) == (
        "第 1 步：铸币不够"
    )


def test_sell_must_be_on_board_and_only_once():
    board = [minion(10, "X", 1, 1)]
    assert validate_plan([sell(11)], ctx_for(board=board)) == "第 1 步：场上没有 #11"
    assert validate_plan([sell(10), sell(10)], ctx_for(board=board)) == "第 2 步：#10 已经卖过"


def test_upgrade_rules():
    assert validate_plan([UPGRADE], ctx_for(gold=5, upgrade_cost=5)) is None
    assert validate_plan([UPGRADE], ctx_for(gold=4, upgrade_cost=5)) == "第 1 步：铸币不够"
    assert validate_plan([UPGRADE], ctx_for(gold=9, upgrade_cost=None)) == (
        "第 1 步：已经是最高等级，不能升本"
    )
    assert validate_plan([UPGRADE, UPGRADE], ctx_for(gold=20, upgrade_cost=5)) == (
        "第 2 步：一个方案只能升本一次"
    )


def test_refresh_must_be_last():
    assert validate_plan([buy(20), REFRESH], ctx_for(gold=4, shop=SHOP)) is None
    assert validate_plan([FREEZE], ctx_for(gold=1)) is None
    last = "第 2 步：刷新要放在最后，刷新之后不能再有动作"
    # 冻结一个还没看到的商店没有意义：刷新之后连冻结也不行（设计文档第 5.4 节）
    assert validate_plan([REFRESH, FREEZE], ctx_for(gold=1)) == last
    assert validate_plan([REFRESH, buy(20)], ctx_for(gold=9, shop=SHOP)) == last
    assert validate_plan([REFRESH, REFRESH], ctx_for(gold=9)) == last
    assert validate_plan([REFRESH], ctx_for(gold=1, refresh_cost=None)) == "第 1 步：现在不能刷新"


def test_freeze_must_be_last():
    assert validate_plan([FREEZE, buy(20)], ctx_for(gold=3, shop=SHOP)) == (
        "第 2 步：冻结之后不能再有动作"
    )


def test_board_space_counts_hand_minions():
    board = [minion(10 + i, f"X{i}", 1, 1) for i in range(6)]
    hand = [hand_minion(30, "H", 1, 1)]
    assert validate_plan([buy(20)], ctx_for(gold=3, shop=SHOP, board=board, hand=hand)) == (
        "场面放不下：执行后会有 8 个随从（最多 7 个）"
    )
    assert (
        validate_plan([sell(10), buy(20)], ctx_for(gold=3, shop=SHOP, board=board, hand=hand))
        is None
    )


def test_triple_frees_two_slots():
    board = [minion(10 + i, f"X{i}", 1, 1) for i in range(5)] + [
        minion(15, "A", 2, 2),
        minion(16, "A", 2, 2),
    ]
    assert validate_plan([buy(20)], ctx_for(gold=3, shop=SHOP, board=board)) is None


def test_spells_use_their_cost_and_take_no_board_space():
    board = [minion(10 + i, f"X{i}", 1, 1) for i in range(7)]
    shop = [shop_spell(40, "S", cost=1)]
    assert validate_plan([buy(40)], ctx_for(gold=1, shop=shop, board=board)) is None
    assert validate_plan([buy(40)], ctx_for(gold=0, shop=shop, board=board)) == "第 1 步：铸币不够"


def test_overfull_start_allows_doing_nothing_but_not_adding():
    board = [minion(10 + i, f"X{i}", 1, 1) for i in range(7)]
    hand = [hand_minion(30, "H", 1, 1)]
    assert validate_plan([], ctx_for(board=board, hand=hand)) is None
    assert validate_plan([buy(20)], ctx_for(gold=3, shop=SHOP, board=board, hand=hand)) == (
        "场面放不下：执行后会有 9 个随从（最多 8 个）"
    )
    assert (
        validate_plan([sell(10), buy(20)], ctx_for(gold=3, shop=SHOP, board=board, hand=hand))
        is None
    )


def hero_power(target=None):
    return Action(type="hero_power", entity_id=target)


HP = hero_power()
BOARD = [minion(10, "X", 1, 1), minion(11, "Y", 2, 2)]


def test_usable_free_hero_power():
    assert validate_plan([HP], ctx_for(gold=0, hero_power_state=power())) is None
    assert (
        validate_plan([buy(20), HP, REFRESH], ctx_for(gold=4, shop=SHOP, hero_power_state=power()))
        is None
    )


def test_hero_power_costs_gold():
    paid = power(cost=2)
    assert validate_plan([HP], ctx_for(gold=2, hero_power_state=paid)) is None
    assert validate_plan([buy(20), HP], ctx_for(gold=4, shop=SHOP, hero_power_state=paid)) == (
        "第 2 步：铸币不够"
    )


def test_hero_power_that_cannot_be_used():
    cases = [
        (None, "第 1 步：没有英雄技能"),
        (power(used=True), "第 1 步：本回合已经用过英雄技能"),
        (
            power(option="unusable", reason="REQ_NOT_EXHAUSTED_HERO_POWER"),
            "第 1 步：本回合已经用过英雄技能",
        ),
        (power(option="absent"), "第 1 步：英雄技能是被动的，不能主动使用"),
        (power(option="absent", passive=False), "第 1 步：英雄技能现在不能用"),
        (power(option="unknown"), "第 1 步：还不知道英雄技能现在能不能用"),
        (
            power(option="unusable", reason="REQ_TARGET_TO_PLAY"),
            "第 1 步：英雄技能现在没有可选的目标",
        ),
        (power(option="unusable", reason="127"), "第 1 步：英雄技能现在不能用（127）"),
    ]
    for hp, problem in cases:
        assert validate_plan([HP], ctx_for(gold=9, hero_power_state=hp)) == problem, hp


def test_not_enough_gold_is_fine_once_the_plan_has_enough():
    short = power(cost=2, option="unusable", reason="REQ_ENOUGH_MANA")
    ctx = ctx_for(gold=1, board=BOARD, hero_power_state=short)
    assert validate_plan([HP], ctx) == "第 1 步：铸币不够"
    assert validate_plan([sell(10), HP], ctx) is None


def test_hero_power_targets():
    aimed = power(targets=(10, 11))
    ctx = ctx_for(gold=3, board=BOARD, hero_power_state=aimed)
    assert validate_plan([hero_power(10)], ctx) is None
    assert validate_plan([hero_power(99)], ctx) == "第 1 步：英雄技能不能以 #99 为目标"
    assert validate_plan([HP], ctx) == "第 1 步：英雄技能要选一个目标"
    assert validate_plan([sell(10), hero_power(10)], ctx) == "第 2 步：#10 已经卖掉了"


def test_target_is_ignored_when_not_needed():
    assert validate_plan([hero_power(99)], ctx_for(hero_power_state=power())) is None


def test_hero_power_order_and_count():
    ctx = ctx_for(gold=9, hero_power_state=power())
    assert validate_plan([REFRESH, HP], ctx) == "第 2 步：刷新要放在最后，刷新之后不能再有动作"
    assert validate_plan([HP, HP], ctx) == "第 2 步：一个方案只能用一次英雄技能"
    assert validate_plan([HP, FREEZE], ctx) is None
    assert validate_plan([FREEZE, HP], ctx) == "第 2 步：冻结之后不能再有动作"


def test_unaffordable_power_that_may_need_a_target_aims_at_my_board():
    # 铸币不够时选项不列目标：要目标（或不知道要不要）就只能选场上没卖掉的随从
    for needs in (True, None):
        short = power(cost=2, option="unusable", reason="REQ_ENOUGH_MANA", needs_target=needs)
        ctx = ctx_for(gold=1, board=BOARD, shop=SHOP, hero_power_state=short)
        assert validate_plan([sell(11), hero_power(10)], ctx) is None
        assert validate_plan([sell(11), HP], ctx) == "第 2 步：英雄技能要选一个目标"
        assert validate_plan([sell(11), hero_power(11)], ctx) == "第 2 步：#11 已经卖掉了"
        assert validate_plan([sell(11), hero_power(20)], ctx) == (
            "第 2 步：英雄技能只能以场上的随从为目标，#20 不在场上"
        )
    known = power(cost=2, option="unusable", reason="REQ_ENOUGH_MANA", needs_target=False)
    ctx = ctx_for(gold=1, board=BOARD, hero_power_state=known)
    assert validate_plan([sell(11), hero_power(99)], ctx) is None  # 不需要目标：忽略编号


def test_already_used_by_options_counts_as_used():
    hp = power(option="unusable", reason="REQ_NOT_EXHAUSTED_HERO_POWER")
    assert validate_plan([HP], ctx_for(hero_power_state=hp)) == "第 1 步：本回合已经用过英雄技能"


def test_normalize_drops_targets_the_power_does_not_need():
    from lscs.advisor.validate import normalize_actions

    actions = (buy(20), hero_power(99))
    assert normalize_actions(actions, ctx_for(hero_power_state=power())) == (buy(20), HP)
    aimed = ctx_for(board=BOARD, hero_power_state=power(targets=(10,)))
    assert normalize_actions((hero_power(10),), aimed) == (hero_power(10),)
    unknown = ctx_for(hero_power_state=power(needs_target=None))
    assert normalize_actions((hero_power(10),), unknown) == (hero_power(10),)
