from helpers import TEST_POOL

from lscs.pipeline import Pipeline
from lscs.state.models import EventType
from lscs.state.tracker import StateTracker


def test_flushes_once_per_timestamp_group(mini_lines):
    updates = []
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: updates.append((s, e)))
    for line in mini_lines:
        pipeline.push(line)
    pipeline.flush()
    groups = {line.split(" ")[1] for line in mini_lines}
    assert len(updates) == len(groups)


def test_parse_error_marks_state_uncertain_and_continues(mini_lines):
    updates = []
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: updates.append((s, e)))
    broken = list(mini_lines)
    idx = next(i for i, line in enumerate(broken) if "tag=RESOURCES value=3" in line)
    broken.insert(
        idx,
        "D 10:00:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=2 tag=RESOURCES value=NOT_A_NUMBER",
    )
    for line in broken:
        pipeline.push(line)
    pipeline.flush()
    final, events = updates[-1]
    assert final.uncertain
    assert EventType.GAME_ENDED in [e.type for e in events]


def test_tracker_exception_marks_uncertain_and_continues(mini_lines):
    class FlakyTracker(StateTracker):
        calls = 0

        def apply(self, changes):
            FlakyTracker.calls += 1
            if FlakyTracker.calls == 5:
                raise RuntimeError("boom")
            super().apply(changes)

    updates = []
    pipeline = Pipeline(FlakyTracker(TEST_POOL), lambda s, e: updates.append((s, e)))
    for line in mini_lines:
        pipeline.push(line)
    pipeline.flush()
    final, events = updates[-1]
    assert final.uncertain
    assert EventType.GAME_ENDED in [e.type for e in events]


def test_on_update_exception_does_not_stop_later_updates(mini_lines):
    received = []

    def on_update(state, events):
        received.append(state.seq)
        if len(received) == 1:
            raise RuntimeError("display broke")

    pipeline = Pipeline(StateTracker(TEST_POOL), on_update)
    for line in mini_lines:
        pipeline.push(line)
    pipeline.flush()
    assert len(received) == len({line.split(" ")[1] for line in mini_lines})


def test_persistent_flush_error_logs_traceback_once(caplog):
    class BrokenTracker(StateTracker):
        message = "坏了"

        def flush(self):
            self._dirty = False
            raise RuntimeError(self.message)

    tracker = BrokenTracker(TEST_POOL)
    pipeline = Pipeline(tracker, lambda s, e: None)
    for _ in range(5):  # watch 每次读不到新行都会 flush
        pipeline.flush()
    tracebacks = [r for r in caplog.records if r.exc_info and r.levelname == "WARNING"]
    assert len(tracebacks) == 1
    assert tracker._uncertain
    tracker.message = "另一个错误"
    pipeline.flush()
    tracebacks = [r for r in caplog.records if r.exc_info and r.levelname == "WARNING"]
    assert len(tracebacks) == 2


FIRST_RECRUIT = "10:00:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION"
OPTIONS = "D 10:00:11.0000000 GameState.DebugPrintOptions() - "
POWER_USABLE = [
    OPTIONS + "id=1",
    OPTIONS + "  option 0 type=END_TURN mainEntity= error=INVALID errorParam=",
    OPTIONS
    + "  option 1 type=POWER mainEntity=[entityName=法力风暴 id=15 zone=PLAY zonePos=0 cardId=TB_BaconShop_HP_054 player=1] error=NONE errorParam=",
]


def with_lines_after(lines, marker, extra):
    i = next(i for i, line in enumerate(lines) if marker in line)
    return lines[: i + 1] + extra + lines[i + 1 :]


def run(lines, flush_after=None):
    updates = []
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: updates.append((s, e)))
    for line in lines:
        pipeline.push(line)
        if flush_after is not None and flush_after in line:
            pipeline.flush()  # watch 读不到新行时 flush
    pipeline.flush()
    return updates


def started(updates):
    return next(
        (s, ev) for s, ev in updates if any(e.type == EventType.RECRUIT_STARTED for e in ev)
    )


def test_options_right_after_recruit_start_join_its_update(mini_lines):
    lines = with_lines_after(mini_lines, FIRST_RECRUIT, POWER_USABLE)
    updates = run(lines)
    state, events = started(updates)
    assert state.me.hero_power.option == "usable"
    assert state.me.gold == 3  # 后面买牌的变化不在这次更新里
    assert len(updates) == len({line.split(" ")[1] for line in mini_lines})


def test_recruit_start_is_not_held_back_past_other_changes(mini_lines):
    # 选项没来、局面先变了（买牌）：招募开始的那次更新照常先发出
    state, _ = started(run(mini_lines))
    assert (state.me.gold, state.me.hand) == (3, ())
    assert state.me.hero_power.option == "unknown"


def test_quiet_poll_releases_the_wait_and_options_follow(mini_lines):
    lines = with_lines_after(mini_lines, FIRST_RECRUIT, POWER_USABLE)
    updates = run(lines, flush_after=FIRST_RECRUIT)
    state, events = started(updates)
    assert state.me.hero_power.option == "unknown"
    i = next(i for i, (s, _) in enumerate(updates) if s is state)
    later, later_events = updates[i + 1]
    assert later.me.hero_power.option == "usable"
    assert [e.type for e in later_events] == [EventType.HERO_POWER_CHANGED]


def test_options_spread_over_two_timestamps_still_join_the_recruit_start(mini_lines):
    later = "D 10:00:11.0010000 GameState.DebugPrintOptions() - "
    split = [
        POWER_USABLE[0],
        POWER_USABLE[1],
        later + POWER_USABLE[2].split(" - ", 1)[1],  # 技能这一行换了时间戳
        "D 10:00:11.0020000 PowerTaskList.DebugPrintPower() -     TAG_CHANGE Entity=15 tag=ATK value=1",
    ]
    updates = run(with_lines_after(mini_lines, FIRST_RECRUIT, split))
    state, events = started(updates)
    assert (state.me.hero_power.option, state.me.hero_power.passive) == ("usable", False)
    assert EventType.HERO_POWER_CHANGED not in [
        e.type for _, ev in updates[updates.index((state, events)) + 1 :] for e in ev
    ]
