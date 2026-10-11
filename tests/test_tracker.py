from lscs.state.models import EventType, Phase, TribesConfidence
from lscs.state.pool import PoolConfig


def event_types(updates):
    return [e.type for _, events in updates for e in events]


def first_with(updates, event_type):
    for state, events in updates:
        for event in events:
            if event.type == event_type:
                return state, event
    raise AssertionError(f"没有 {event_type}")


def test_event_sequence(run_log, mini_lines):
    types = event_types(run_log(mini_lines))
    key = [
        t
        for t in types
        if t
        not in (
            EventType.SHOP_CHANGED,
            EventType.BOARD_CHANGED,
            EventType.HAND_CHANGED,
            EventType.GOLD_CHANGED,
        )
    ]
    assert key == [
        EventType.GAME_STARTED,
        EventType.HERO_CHOSEN,
        EventType.RECRUIT_STARTED,
        EventType.COMBAT_STARTED,
        EventType.COMBAT_ENDED,
        EventType.RECRUIT_STARTED,
        EventType.GAME_ENDED,
    ]


def test_hero_chosen_carries_hero(run_log, mini_lines):
    _, event = first_with(run_log(mini_lines), EventType.HERO_CHOSEN)
    assert event.data == {"hero": "TB_BaconShop_HERO_49"}


def test_recruit_changes_emit_diff_events(run_log, mini_lines):
    updates = run_log(mini_lines)
    buy = next(events for state, events in updates if state.me and state.me.hand)
    assert {e.type for e in buy} == {
        EventType.SHOP_CHANGED,
        EventType.HAND_CHANGED,
        EventType.GOLD_CHANGED,
    }


def test_combat_identifies_opponent_and_captures_board_before_deaths(run_log, mini_lines):
    updates = run_log(mini_lines)
    _, started = first_with(updates, EventType.COMBAT_STARTED)
    assert started.data == {"opponent": 3, "hero": "TB_BaconShop_HERO_75"}
    state, ended = first_with(updates, EventType.COMBAT_ENDED)
    assert ended.data == {"opponent": 3}
    opp = state.opponents[3]
    assert [(m.card_id, m.golden, m.keywords) for m in opp.last_board] == [
        ("BG21_029_G", True, ()),
        ("BGS_116", False, ("TAUNT",)),
    ]
    assert opp.last_seen_turn == 1
    assert state.me.health == 27
    # 对手阵容计入卡池估算：金色小鬼占 3 张
    assert state.pool_estimate["BG21_029"] == 13


def test_tribes_accumulate_across_turns(run_log, mini_lines):
    state, _ = first_with(run_log(mini_lines), EventType.GAME_ENDED)
    assert state.tribes == {"DEMON", "PIRATE", "ELEMENTAL", "NAGA"}
    assert state.tribes_confidence == TribesConfidence.INFERRING
    small = PoolConfig(tribe_count=4, copies={1: 16, 2: 15})
    state, _ = first_with(run_log(mini_lines, pool=small), EventType.GAME_ENDED)
    assert state.tribes_confidence == TribesConfidence.CONFIRMED


def test_game_ended_reports_place(run_log, mini_lines):
    state, event = first_with(run_log(mini_lines), EventType.GAME_ENDED)
    assert event.data == {"place": 5}
    assert state.phase == Phase.GAME_OVER


def test_second_game_in_same_log_starts_fresh(run_log, mini_lines):
    updates = run_log(mini_lines + mini_lines)
    types = event_types(updates)
    assert types.count(EventType.GAME_STARTED) == 2
    assert types.count(EventType.GAME_ENDED) == 2
    second_start = [
        i for i, (_, ev) in enumerate(updates) if any(e.type == EventType.GAME_STARTED for e in ev)
    ][1]
    state, _ = updates[second_start]
    assert state.tribes == frozenset()
    assert all(o.last_board == () for o in state.opponents.values())


def test_non_battlegrounds_game_emits_nothing(run_log, mini_lines):
    lines = [line.replace("GT_BATTLEGROUNDS", "GT_RANKED") for line in mini_lines]
    assert event_types(run_log(lines)) == []


def test_seq_increases(run_log, mini_lines):
    seqs = [state.seq for state, _ in run_log(mini_lines)]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


def _swap_player_ids(lines):
    out = []
    for line in lines:
        if "DebugPrintGame() - PlayerID=1, PlayerName=Me#1234" in line:
            line = line.replace("PlayerID=1", "PlayerID=2")
        elif "DebugPrintGame() - PlayerID=2, PlayerName=The Innkeeper" in line:
            line = line.replace("PlayerID=2", "PlayerID=1")
        out.append(line)
    return out


def test_second_game_with_different_player_id_is_parsed(run_log, mini_lines):
    from lscs.parsing.feed import PowerFeed

    game2 = _swap_player_ids(mini_lines)
    feed = PowerFeed()
    for line in mini_lines + game2:
        feed.feed(line)
    assert feed.errors == 0
    # 新对局开始后只保留当前对局的数据包，避免内存随局数增长
    assert len(feed._packets) < 1.5 * len(mini_lines)

    updates = run_log(mini_lines + game2)
    ends = [s for s, e in updates if s.phase == Phase.GAME_OVER]
    assert [s.final_place for s in ends] == [5, 5]
    recruits = [s for s, e in updates if s.phase == Phase.RECRUIT]
    assert recruits and all(s.me is not None for s in recruits)
