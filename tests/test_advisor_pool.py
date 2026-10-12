"""查过卡牌库的卡池估计（对手一览与卡池估计设计文档第 4 节）。"""

from factory import hand_minion, make_state, minion, shop_minion
from helpers import (
    CC0_POOL,
    FIXTURES,
    NEEDS_USER_LOG,
    REAL_LOG,
    USER_LOG,
    recruit_at,
    replay_log,
)

from lscs.advisor.context import build_context
from lscs.advisor.pool import PoolEstimator, PoolNote, pool_notes
from lscs.cards.db import CardDB
from lscs.state.models import OpponentView, TribesConfidence

COPIES = {1: 16, 2: 15, 3: 13}
RAW = [
    {
        "id": "IMP",
        "dbfId": 1,
        "name": "小鬼",
        "type": "MINION",
        "techLevel": 1,
        "races": ["DEMON"],
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "IMP_G",
        "dbfId": 2,
        "name": "小鬼",
        "type": "MINION",
        "techLevel": 1,
        "races": ["DEMON"],
        "battlegroundsNormalDbfId": 1,
    },
    {
        "id": "TB_BaconUps_1",
        "dbfId": 3,
        "name": "小鬼",
        "type": "MINION",
        "techLevel": 1,
        "races": ["DEMON"],
        "battlegroundsNormalDbfId": 1,
    },
    {"id": "TOKEN", "dbfId": 4, "name": "触须", "type": "MINION", "techLevel": 1},
    {
        "id": "BOAR",
        "dbfId": 5,
        "name": "野猪",
        "type": "MINION",
        "techLevel": 2,
        "races": ["BEAST"],
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "NEUTRAL",
        "dbfId": 6,
        "name": "中立",
        "type": "MINION",
        "techLevel": 3,
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "ALL",
        "dbfId": 7,
        "name": "全能",
        "type": "MINION",
        "techLevel": 2,
        "races": ["ALL"],
        "isBattlegroundsPoolMinion": True,
    },
    {
        "id": "BIG",
        "dbfId": 8,
        "name": "七本",
        "type": "MINION",
        "techLevel": 7,
        "isBattlegroundsPoolMinion": True,
    },
]
CARDS = CardDB(RAW)
ESTIMATOR = PoolEstimator(CARDS, COPIES)


def opponent(player_id, board, health=20):
    return OpponentView(
        player_id=player_id,
        hero_card_id="HERO",
        health=health,
        armor=0,
        place=player_id,
        tavern_tier=3,
        last_board=tuple(board),
        last_seen_turn=4,
    )


def state_with(board=(), hand=(), shop=(), opponents=(), tribes=frozenset(), confirmed=None):
    """confirmed 默认看有没有给本局种族。"""
    state = make_state(board=board, hand=hand, shop=shop, tribes=tribes)
    confirmed = bool(tribes) if confirmed is None else confirmed
    confidence = TribesConfidence.CONFIRMED if confirmed else TribesConfidence.INFERRING
    return state.model_copy(
        update={
            "opponents": {o.player_id: o for o in opponents},
            "tribes_confidence": confidence,
        }
    )


def test_golden_cards_count_three_in_every_spelling():
    state = state_with(
        board=[
            minion(10, "IMP", 1, 1),
            minion(11, "IMP_G", 2, 2, golden=True),  # 普通卡 ID + _G
            minion(12, "IMP", 2, 2, golden=True),  # 只有 PREMIUM
        ],
        opponents=[opponent(2, [minion(20, "TB_BaconUps_1", 2, 2, golden=True)])],  # 旧式
    )
    view = ESTIMATOR.view(state)
    assert view.taken == {"IMP": 10}
    assert view.remaining("IMP") == 6
    assert view.remaining("IMP_G") == 6 and view.remaining("TB_BaconUps_1") == 6


def test_tokens_are_not_counted_and_have_no_estimate():
    view = ESTIMATOR.view(state_with(board=[minion(10, "TOKEN", 2, 2)]))
    assert view.taken == {}
    assert view.remaining("TOKEN") is None
    assert view.remaining("NOPE") is None  # 卡牌库不认识


def test_lobby_tribes_filter_only_once_confirmed():
    board = [minion(10, "BOAR", 2, 2, tier=2), minion(11, "BOAR", 2, 2, tier=2)]
    confirmed = ESTIMATOR.view(state_with(board=board, tribes={"DEMON"}))
    assert confirmed.taken == {}
    assert confirmed.remaining("BOAR") is None  # 本局没有野兽
    assert (confirmed.remaining("NEUTRAL"), confirmed.remaining("ALL")) == (13, 15)
    inferring = ESTIMATOR.view(state_with(board=board, tribes={"DEMON"}, confirmed=False))
    assert inferring.remaining("BOAR") == 13  # 种族还在推断：不过滤
    unknown = ESTIMATOR.view(state_with(board=board, confirmed=True))
    assert unknown.remaining("BOAR") == 13  # 说是确认了却没有种族：也不过滤


def test_eliminated_opponents_are_not_deducted():
    state = state_with(
        opponents=[
            opponent(2, [minion(20, "IMP", 1, 1)]),
            opponent(3, [minion(30, "IMP", 1, 1)], health=0),
            opponent(4, [minion(40, "IMP_G", 2, 2, golden=True)], health=-5),
        ]
    )
    assert ESTIMATOR.view(state).remaining("IMP") == 15


def test_unseen_pool_minion_is_full_and_unknown_tier_is_none():
    view = ESTIMATOR.view(state_with())
    assert view.remaining("NEUTRAL") == 13
    assert view.remaining("BIG") is None  # 7 本不在 copies 里


def notes_for(state, look_for=(), view=None):
    ctx = build_context(state, 3)
    view = view or ESTIMATOR.view(state)
    shop = [s.card_id for s in state.shop]
    return pool_notes(view, ctx, shop, look_for, CARDS.name)


def test_pool_notes_pairs_near_pairs_then_look_for():
    state = state_with(
        board=[
            minion(10, "NEUTRAL", 3, 3, tier=3),
            minion(11, "IMP", 1, 1),
            minion(12, "IMP", 1, 1),
        ],
        hand=[hand_minion(13, "ALL", 2, 1, tier=2)],
        shop=[shop_minion(20, "ALL", 2, 1, tier=2), shop_minion(21, "BOAR", 2, 2, tier=2)],
        tribes={"DEMON", "BEAST"},
    )
    notes = notes_for(state, look_for=("野猪", "小鬼(金)", "没有这张", "触须"))
    assert notes == (
        PoolNote("IMP", "小鬼", have=2, left=14),
        PoolNote("ALL", "全能", have=1, left=14, shop=1),
        PoolNote("BOAR", "野猪", have=0, left=15, shop=1),
    )
    assert notes[0].pair and notes[1].pair and not notes[2].pair


def test_pool_notes_skip_unknown_cards_and_cap_at_eight():
    raw = [
        {"id": f"N{i}", "name": f"牌{i}", "techLevel": 1, "isBattlegroundsPoolMinion": True}
        for i in range(10)
    ]
    cards = CardDB(raw)
    board = [minion(10 + i, f"N{i}", 1, 1) for i in range(5)] * 2
    board.append(minion(99, "NOT_IN_DB", 1, 1))
    board.append(minion(98, "NOT_IN_DB", 1, 1))
    state = state_with(board=board)
    view = PoolEstimator(cards, COPIES).view(state)
    ctx = build_context(state, 3)
    names = [f"牌{i}" for i in range(10)]
    notes = pool_notes(view, ctx, [], names, cards.name)
    assert [n.card_id for n in notes] == ["N0", "N1", "N2", "N3", "N4", "N5", "N6", "N7"]
    assert notes[0] == PoolNote("N0", "牌0", have=2, left=14)


def test_pool_notes_without_context_are_empty():
    view = ESTIMATOR.view(state_with())
    assert pool_notes(view, None, [], ("小鬼",), CARDS.name) == ()


MINI_CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")


@NEEDS_USER_LOG
def test_user_recording_turn_13():
    from lscs.cli import DEFAULT_POOL
    from lscs.state.pool import load_pool_config

    pool = load_pool_config(DEFAULT_POOL)
    state = recruit_at(replay_log(USER_LOG, pool), 13)
    view = PoolEstimator(MINI_CARDS, pool.copies).view(state)
    assert view.remaining("BG36_116") == 14
    assert view.remaining("BG36_099") == 11  # 我方一张金色 + 对手 2 一张
    assert view.remaining("BG23_HERO_201pt5") is None  # 厄祖玛特的触须：衍生物
    assert view.remaining("BGFYM_002t") is None  # 畸变触手：衍生物


def test_cc0_old_golden_ids_are_mapped():
    state = recruit_at(replay_log(REAL_LOG, CC0_POOL), 15)
    view = PoolEstimator(MINI_CARDS, CC0_POOL.copies).view(state)
    # 对手 1 的 TB_BaconUps_159（金色爆裂飓风）映射到 BGS_119，算 3 张
    assert view.remaining("BGS_119") == 13
    assert view.remaining("TB_BaconUps_159") == 13
