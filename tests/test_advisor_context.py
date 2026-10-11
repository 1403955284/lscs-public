from factory import hand_minion, hand_spell, make_state, minion, shop_minion, shop_spell

from lscs.advisor.context import build_context
from lscs.state.models import Phase


def test_no_context_outside_recruit():
    assert build_context(make_state(phase=Phase.COMBAT), buy_cost=3) is None


def test_basic_facts():
    state = make_state(
        turn=5,
        gold=7,
        tier=2,
        health=25,
        armor=4,
        upgrade_cost=6,
        refresh_cost=2,
        board=[minion(10, "BG21_029", 1, 1)],
        hand=[hand_minion(11, "BGS_061", 3, 1), hand_spell(12, "BG28_500")],
    )
    ctx = build_context(state, buy_cost=3)
    assert (ctx.turn, ctx.gold, ctx.tier, ctx.effective_health) == (5, 7, 2, 29)
    assert (ctx.buy_cost, ctx.upgrade_cost, ctx.refresh_cost) == (3, 6, 2)
    assert (ctx.board_minions, ctx.hand_minions) == (1, 1)


def test_copies_count_board_and_hand_but_not_golden():
    state = make_state(
        board=[minion(10, "BG21_029", 1, 1), minion(11, "BG21_029_G", 2, 2, golden=True)],
        hand=[hand_minion(12, "BG21_029", 1, 1), hand_minion(13, "BGS_061", 3, 1)],
    )
    ctx = build_context(state, buy_cost=3)
    assert ctx.copies == {"BG21_029": 2, "BGS_061": 1}


def test_main_tribe_counts_board_and_hand_within_lobby():
    state = make_state(
        tribes={"BEAST", "DEMON"},
        board=[
            minion(10, "A", 1, 1, races=["DEMON"]),
            minion(11, "B", 1, 1, races=["MURLOC"]),  # 不在本局种族里
            minion(12, "C", 1, 1, races=["MURLOC"]),
        ],
        hand=[hand_minion(13, "D", 1, 1, races=["DEMON"])],
    )
    assert build_context(state, buy_cost=3).main_tribe == "DEMON"


def test_main_tribe_tie_breaks_on_total_stats_and_ignores_all():
    state = make_state(
        board=[
            minion(10, "A", 1, 1, races=["DEMON"]),
            minion(11, "B", 5, 5, races=["BEAST"]),
            minion(12, "C", 9, 9, races=["ALL"]),
        ],
    )
    assert build_context(state, buy_cost=3).main_tribe == "BEAST"
    assert build_context(make_state(), buy_cost=3).main_tribe is None


def test_price_of_spells_and_minions():
    state = make_state(
        shop=[
            shop_minion(20, "A", 1, 1),
            shop_spell(21, "S", cost=1),
            shop_spell(22, "T", cost=None),
        ]
    )
    ctx = build_context(state, buy_cost=3)
    assert [ctx.price(item) for item in ctx.shop] == [3, 1, 3]
