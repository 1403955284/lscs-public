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
        EventType.HEROES_OFFERED,  # 备选英雄（米尔豪斯）在手牌区
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


def _recruit_tracker(mini_lines):
    """喂到第 1 回合招募开始并 flush，返回 tracker。"""
    from helpers import TEST_POOL

    from lscs.parsing.feed import PowerFeed
    from lscs.state.tracker import StateTracker

    feed, tracker = PowerFeed(), StateTracker(TEST_POOL)
    for line in mini_lines:
        tracker.apply(feed.feed(line))
        if "tag=STEP value=MAIN_ACTION" in line:
            break
    return tracker


def test_waiting_for_options_after_recruit_starts(mini_lines):
    from lscs.parsing.feed import OptionsStarted

    tracker = _recruit_tracker(mini_lines)
    assert tracker.awaiting_options
    tracker.apply([OptionsStarted()])
    assert not tracker.awaiting_options
    tracker = _recruit_tracker(mini_lines)
    tracker.flush()
    assert not tracker.awaiting_options  # 招募开始已经发出去了，不再等


def test_hero_power_change_emits_event(mini_lines):
    from lscs.parsing.feed import OptionListed, OptionsStarted

    tracker = _recruit_tracker(mini_lines)
    tracker.flush()
    tracker.apply([OptionsStarted(), OptionListed("option", 15, None)])
    state, events = tracker.flush()
    assert state.me.hero_power.option == "usable"
    assert [e.type for e in events] == [EventType.HERO_POWER_CHANGED]


# ---- 选英雄（合成日志 hero_select.log）----


def hero_lines():
    from helpers import HERO_LOG

    from lscs.replay import read_log_lines

    return list(read_log_lines(HERO_LOG))


def offers(updates):
    return [
        (state, event)
        for state, events in updates
        for event in events
        if event.type == EventType.HEROES_OFFERED
    ]


def offered(state):
    return [(h.entity_id, h.card_id, h.position, h.armor) for h in state.hero_select.offered]


def test_heroes_offered_then_rerolled_then_chosen(run_log):
    updates = run_log(hero_lines())
    found = offers(updates)
    assert [e.data for _, e in found] == [{"count": 4, "changed": 0}, {"count": 4, "changed": 1}]
    (first, _), (second, _) = found
    assert offered(first) == [
        (11, "BG21_HERO_000", 1, 10),
        (12, "BG28_HERO_800", 2, 18),
        (13, "BG33_HERO_001", 3, 19),
        (14, "TB_BaconShop_HERO_91", 4, 17),
    ]
    hero = first.hero_select.offered[0]
    assert (hero.health, hero.hero_power_dbf, hero.locked) == (30, 73941, False)
    assert [h.locked for h in first.hero_select.offered] == [False, False, True, True]
    assert first.hero_select.reroll is True
    # 重随：12 离开手牌，新实体 15 补到同一位置；仍按位置排序
    assert offered(second) == [
        (11, "BG21_HERO_000", 1, 10),
        (15, "BG22_HERO_003", 2, 12),
        (13, "BG33_HERO_001", 3, 19),
        (14, "TB_BaconShop_HERO_91", 4, 17),
    ]
    assert first.hero_select.signature() != second.hero_select.signature()
    assert all(e.turn == 0 for _, e in found)


def test_heroes_offered_order_of_events(run_log):
    types = event_types(run_log(hero_lines()))
    assert [t for t in types if t != EventType.GOLD_CHANGED][:5] == [
        EventType.GAME_STARTED,
        EventType.HEROES_OFFERED,
        EventType.HEROES_OFFERED,
        EventType.HERO_CHOSEN,
        EventType.RECRUIT_STARTED,
    ]


def test_hero_select_is_none_after_the_choice_and_in_recruit(run_log):
    updates = run_log(hero_lines())
    chosen = next(
        i for i, (_, ev) in enumerate(updates) if EventType.HERO_CHOSEN in {e.type for e in ev}
    )
    assert all(state.hero_select is not None for state, _ in updates[1:chosen])
    assert all(state.hero_select is None for state, _ in updates[chosen:])
    assert updates[chosen][0].phase == Phase.HERO_SELECT  # 选完后还要等一会儿才到第 1 回合
    assert updates[-1][0].phase == Phase.RECRUIT


def test_other_players_heroes_are_not_offered_to_me(run_log):
    # 其他玩家的英雄（SETASIDE、由 Bob 控制）陆续出现，不算备选，也不重新发事件
    lines = hero_lines()
    at = next(i for i, line in enumerate(lines) if line.startswith("D 10:00:20"))  # 选择之前
    updates = run_log(lines[:at])
    assert len(updates) == 4 and len(offers(updates)) == 2
    assert all(
        7 not in {h.entity_id for h in state.hero_select.offered}
        for state, _ in updates
        if state.hero_select is not None
    )


def test_reroll_by_changing_the_card_of_the_same_entity(run_log):
    lines = [line for line in hero_lines() if not line.startswith("D 10:00:05")]
    power = "D 10:00:05.0000000 GameState.DebugPrintPower() - "
    reroll = [
        power + "CHANGE_ENTITY - Updating Entity=12 CardID=BG22_HERO_003",
        power + "    tag=ARMOR value=12",
        power + "    tag=HERO_POWER value=80248",
    ]
    at = next(i for i, line in enumerate(lines) if line.startswith("D 10:00:20"))
    found = offers(run_log(lines[:at] + reroll))
    assert [e.data["changed"] for _, e in found] == [0, 1]
    assert offered(found[1][0])[1] == (12, "BG22_HERO_003", 2, 12)
    assert found[1][0].hero_select.offered[1].hero_power_dbf == 80248


def test_reconnect_during_hero_select_offers_again(run_log):
    lines = hero_lines()
    at = next(i for i, line in enumerate(lines) if line.startswith("D 10:00:03"))
    # 重连：重新写一遍 CREATE_GAME 和完整的实体快照，这时英雄已经在手牌区
    again = [
        line.replace("10:00:00.0000000", "10:01:00.0000000").replace(
            "10:00:01.0000000", "10:01:00.0000000"
        )
        for line in lines[:at]
    ]
    updates = run_log(lines[:at] + again)
    _, events = updates[-1]
    assert [e.type for e in events] == [EventType.GAME_STARTED, EventType.HEROES_OFFERED]
    assert events[1].data == {"count": 4, "changed": 0}  # 新对局从头算


def test_mini_log_offers_its_single_hero(run_log, mini_lines):
    state, event = first_with(run_log(mini_lines), EventType.HEROES_OFFERED)
    assert event.data == {"count": 1, "changed": 0}
    assert offered(state) == [(14, "TB_BaconShop_HERO_49", 0, 0)]
    assert state.hero_select.reroll is False
