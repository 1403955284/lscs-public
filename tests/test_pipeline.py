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
