"""用 HearthSim 公开的真实对局（build 139963，CC0 协议）验证状态翻译。"""

import pytest
from helpers import REAL_LOG

from lscs.replay import read_log_lines
from lscs.state.models import EventType, Phase, TribesConfidence
from lscs.state.pool import PoolConfig

POOL = PoolConfig(tribe_count=5, copies={1: 16, 2: 15, 3: 13, 4: 11, 5: 9, 6: 7})


@pytest.fixture(scope="module")
def updates():
    from lscs.pipeline import Pipeline
    from lscs.state.tracker import StateTracker

    result = []
    pipeline = Pipeline(StateTracker(POOL), lambda s, e: result.append((s, e)))
    for line in read_log_lines(REAL_LOG):
        pipeline.push(line)
    pipeline.flush()
    return result


def at(updates, event_type, turn):
    for state, events in updates:
        for event in events:
            if event.type == event_type and event.turn == turn:
                return state, event
    raise AssertionError(f"没有第 {turn} 回合的 {event_type}")


def count(updates, event_type):
    return sum(1 for _, events in updates for e in events if e.type == event_type)


def test_no_parse_errors(updates):
    assert not any(state.uncertain for state, _ in updates)


def test_whole_game_structure(updates):
    assert count(updates, EventType.GAME_STARTED) == 1
    assert count(updates, EventType.RECRUIT_STARTED) == 16
    assert count(updates, EventType.COMBAT_STARTED) == 16
    assert count(updates, EventType.COMBAT_ENDED) == 16
    final, events = updates[-1]
    assert final.phase == Phase.GAME_OVER
    assert final.final_place == 1


def test_hero_chosen(updates):
    _, event = at(updates, EventType.HERO_CHOSEN, 0)
    assert event.data["hero"] == "TB_BaconShop_HERO_49_SKIN_A"


def test_turn_1_recruit(updates):
    state, _ = at(updates, EventType.RECRUIT_STARTED, 1)
    me = state.me
    assert (me.gold, me.tavern_tier, me.upgrade_cost, me.refresh_cost, me.health) == (
        3,
        1,
        6,
        2,
        40,
    )
    assert me.hero_power_card_id == "TB_BaconShop_HP_054"
    assert [s.card_id for s in state.shop] == ["BG21_029", "BGS_061", "BG21_029"]
    assert me.board == () and me.hand == ()
    assert state.next_opponent_id == 4


def test_turn_5_recruit(updates):
    state, _ = at(updates, EventType.RECRUIT_STARTED, 5)
    me = state.me
    assert (me.gold, me.tavern_tier, me.upgrade_cost) == (7, 2, 6)
    assert [s.card_id for s in state.shop] == ["BG_TID_713", "BGS_055", "BGS_106", "BG23_009"]
    assert [m.card_id for m in me.board] == [
        "BG21_029",
        "BGS_001",
        "BG_TID_713",
        "BG_TID_713",
        "BGS_055",
        "BGS_127",
    ]
    assert (me.board[0].atk, me.board[0].health) == (3, 3)
    assert [h.card_id for h in me.hand] == ["FP1_024"]


def test_turn_5_combat_opponent_board(updates):
    state, event = at(updates, EventType.COMBAT_ENDED, 5)
    assert event.data["opponent"] == 6
    opp = state.opponents[6]
    assert opp.hero_card_id == "TB_BaconShop_HERO_75"
    assert [m.card_id for m in opp.last_board] == [
        "BG22_202",
        "BGS_014",
        "BGS_060",
        "BGS_014",
        "BGS_001",
    ]
    assert opp.last_seen_turn == 5


def test_tribes_inferred_by_turn_8(updates):
    state, _ = at(updates, EventType.RECRUIT_STARTED, 7)
    assert state.tribes_confidence == TribesConfidence.INFERRING
    state, _ = at(updates, EventType.RECRUIT_STARTED, 8)
    assert state.tribes == {"DEMON", "ELEMENTAL", "MURLOC", "NAGA", "PIRATE"}
    assert state.tribes_confidence == TribesConfidence.CONFIRMED


def test_seven_opponents_on_leaderboard(updates):
    state, _ = at(updates, EventType.RECRUIT_STARTED, 1)
    assert len(state.opponents) == 7
    places = [o.place for o in state.opponents.values()]
    assert len(set(places)) == 7 and set(places) <= set(range(1, 9))
