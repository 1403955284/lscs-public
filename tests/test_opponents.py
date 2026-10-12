"""对手一览（对手一览与卡池估计设计文档第 5 节）。"""

from factory import make_state, minion
from helpers import CC0_POOL, NEEDS_USER_LOG, REAL_LOG, USER_LOG, recruit_at, replay_log

from lscs.advisor.context import main_tribe_of
from lscs.advisor.opponents import summarize_opponents
from lscs.state.models import OpponentView

AUTO = object()


def opponent(player_id, *, health=30, armor=0, place=AUTO, tier=3, board=(), seen=None):
    """名次默认等于玩家编号。"""
    return OpponentView(
        player_id=player_id,
        hero_card_id=f"HERO_{player_id}",
        health=health,
        armor=armor,
        place=player_id if place is AUTO else place,
        tavern_tier=tier,
        last_board=tuple(board),
        last_seen_turn=seen,
    )


def overview(*opponents, turn=6, next_id=None, tribes=frozenset()):
    state = make_state(turn=turn, tribes=tribes).model_copy(
        update={"opponents": {o.player_id: o for o in opponents}, "next_opponent_id": next_id}
    )
    return summarize_opponents(state)


def by_id(view):
    return {s.player_id: s for s in view.opponents}


def test_main_tribe_counts_then_stats_then_name():
    board = [
        minion(1, "A", 5, 5, races=["MURLOC"]),
        minion(2, "B", 1, 1, races=["MURLOC"]),
        minion(3, "C", 3, 3, races=["DEMON"]),
        minion(4, "D", 4, 4, races=["DEMON"]),
        minion(5, "E", 9, 9, races=["ALL"]),
    ]
    assert main_tribe_of(board, frozenset()) == ("DEMON", 2)  # 都是 2 个：恶魔身材高
    assert main_tribe_of(board, frozenset({"MURLOC"})) == ("MURLOC", 2)  # 不在本局的不算
    assert main_tribe_of([minion(1, "X", 1, 1)], frozenset()) == (None, 0)


def test_summary_of_a_seen_board():
    board = [
        minion(1, "LOW", 1, 1, tier=1, races=["PIRATE"]),
        minion(2, "BIG", 20, 20, tier=2, races=["PIRATE"]),
        minion(3, "HIGH", 2, 2, tier=5, races=["DRAGON"]),
        minion(4, "GOLD", 2, 2, tier=1, golden=True, races=["PIRATE"]),
        minion(5, "PLAIN", 1, 1, tier=1),
    ]
    view = overview(opponent(2, place=1, tier=5, board=board, seen=4))
    (s,) = view.opponents
    assert (s.minions, s.main_tribe, s.tribe_count, s.total_stats, s.golden) == (
        5,
        "PIRATE",
        3,
        52,
        1,
    )
    # 金色优先，再看酒馆等级、身材
    assert [m.card_id for m in s.key_cards] == ["GOLD", "HIGH", "BIG"]
    assert s.seen_turn == 4 and s.alive


def test_threat_parts_caps_and_rounding():
    strong = opponent(2, health=40, armor=10, tier=7, board=[minion(1, "A", 10, 10)], seen=5)
    weak = opponent(3, health=10, tier=3, board=[minion(2, "B", 5, 5)], seen=6)
    view = by_id(overview(strong, weak, turn=6))
    # 场面：strong 20×1.1=22 是最大的 → 60；等级、血量各封顶 20
    assert view[2].threat == 100
    # weak：60×10/22=27.27；等级 20×3/6=10；血量 20×10/40=5 → 42.27
    assert view[3].threat == 42
    half = opponent(4, health=1, tier=0, board=[minion(3, "C", 1, 1)], seen=6)  # 场面 60×2/4=30
    view = by_id(
        overview(opponent(5, health=1, tier=0, board=[minion(4, "D", 2, 2)], seen=6), half)
    )
    assert view[4].threat == 31  # 30 + 0 + 0.5 → 四舍五入是 31（不是银行家舍入的 30）


def test_old_boards_are_assumed_to_have_grown():
    old = opponent(2, health=1, tier=0, board=[minion(1, "A", 5, 5)], seen=1)  # 10×min(2, 2)=20
    fresh = opponent(3, health=1, tier=0, board=[minion(2, "B", 8, 8)], seen=12)  # 16
    view = by_id(overview(old, fresh, turn=12))
    assert (view[2].threat, view[3].threat) == (61, 49)  # 场面 60、48，血量各 0.5
    assert view[2].strongest


def test_unseen_opponents_are_estimated_from_their_tier():
    view = by_id(overview(opponent(2, health=20, tier=4), opponent(3, health=20, tier=None)))
    assert view[2].threat == 20 + 13 + 10  # min(30, 5×4) + 20×4/6 + 20×20/40
    assert view[3].threat == 10  # 等级不知道：只有血量分
    assert view[2].seen_turn is None and view[2].key_cards == ()


def test_eliminated_opponents_have_no_threat_and_are_never_strongest():
    board = [minion(1, "A", 50, 50)]
    gone = opponent(2, health=-4, place=8, tier=6, board=board, seen=3)
    alive = opponent(3, health=1, tier=1, board=[minion(2, "B", 1, 1)], seen=5)
    view = by_id(overview(gone, alive, turn=6, next_id=2))
    assert (view[2].threat, view[2].strongest, view[2].alive) == (0, False, False)
    assert view[2].next  # 打它的残影
    assert view[3].strongest and not view[3].next


def test_strongest_ties_go_to_the_better_place_then_player_id():
    view = by_id(overview(opponent(2, place=4), opponent(3, place=2), opponent(4, place=2)))
    assert [p for p, s in view.items() if s.strongest] == [3]
    view = by_id(overview(opponent(2, place=None), opponent(5, place=None)))
    assert [p for p, s in view.items() if s.strongest] == [2]


def test_order_contested_and_unknown():
    pirates = [minion(1, "P", 1, 1, races=["PIRATE"])]
    dragons = [minion(2, "D", 1, 1, races=["DRAGON"])]
    view = overview(
        opponent(2, place=None, board=pirates, seen=3),
        opponent(3, place=3, board=dragons, seen=3),
        opponent(4, place=1),
        opponent(5, place=7, health=0, board=pirates, seen=2),
        opponent(6, place=2, board=pirates, seen=4),
        opponent(7, place=8, health=-2),
        opponent(8, place=4, board=[minion(3, "N", 1, 1)], seen=5),  # 中立：没有主种族
    )
    assert [s.player_id for s in view.opponents] == [4, 6, 3, 8, 2, 5, 7]
    assert view.contested == (("PIRATE", 2), ("DRAGON", 1))
    assert view.unknown == 1


def test_no_opponents():
    view = overview()
    assert (view.opponents, view.contested, view.unknown) == ((), (), 0)


@NEEDS_USER_LOG
def test_user_recording_turn_10_and_13():
    from lscs.cli import DEFAULT_POOL
    from lscs.state.pool import load_pool_config

    updates = replay_log(USER_LOG, load_pool_config(DEFAULT_POOL))
    view = summarize_opponents(recruit_at(updates, 10))
    opps = by_id(view)
    strongest = [s for s in view.opponents if s.strongest]
    assert [s.player_id for s in strongest] == [3] and strongest[0].threat >= 90
    second = opps[2]
    assert (second.main_tribe, second.tribe_count, second.total_stats, second.seen_turn) == (
        "ABERRATION",
        5,
        95,
        9,
    )
    assert (opps[1].tavern_tier, opps[1].golden) == (1, 1)
    assert view.contested == (("ABERRATION", 4), ("DRAGON", 1))
    assert view.unknown == 1
    ghost = [s for s in view.opponents if s.next]
    assert [(s.player_id, s.alive) for s in ghost] == [(5, False)]
    view = summarize_opponents(recruit_at(updates, 13))
    assert [s.player_id for s in view.opponents if s.alive] == [3, 2]  # 按名次
    assert [s.player_id for s in view.opponents if s.strongest] == [3]


def test_cc0_turn_11():
    view = summarize_opponents(recruit_at(replay_log(REAL_LOG, CC0_POOL), 11))
    opps = by_id(view)
    first = opps[1]
    assert first.strongest
    assert (first.seen_turn, first.minions, first.main_tribe, first.total_stats) == (
        10,
        7,
        "ELEMENTAL",
        230,
    )
    assert not opps[7].alive and opps[7].minions == 0 and opps[7].seen_turn is None


def test_an_empty_captured_board_counts_as_unseen_for_the_threat():
    # 截到的阵容是空的（对手卖光了随从）：场面分按酒馆等级估，不是 0
    empty = opponent(2, health=20, tier=4, board=(), seen=5)
    view = by_id(overview(empty, opponent(3, health=20, tier=4), turn=6))
    assert view[2].threat == view[3].threat == 20 + 13 + 10
    assert view[2].empty_board and not view[3].empty_board
    assert view[2].seen_turn == 5 and view[2].minions == 0
