import gzip

from lscs.replay import MAX_SLEEP, line_seconds, paced, read_log_lines


def test_read_plain_and_gzip(tmp_path):
    plain = tmp_path / "a.log"
    plain.write_text("x\r\ny\n", encoding="utf-8")
    packed = tmp_path / "a.log.gz"
    with gzip.open(packed, "wt", encoding="utf-8") as f:
        f.write("x\ny\n")
    assert list(read_log_lines(plain)) == ["x", "y"]
    assert list(read_log_lines(packed)) == ["x", "y"]


def test_line_seconds():
    assert line_seconds("D 01:02:03.5000000 GameState.X() - y") == 3723.5
    assert line_seconds("garbage") is None


def test_paced_without_speed_never_sleeps():
    sleeps = []
    lines = ["D 10:00:00.0 a", "D 10:00:05.0 b"]
    assert list(paced(lines, None, sleep=sleeps.append)) == lines
    assert sleeps == []


def test_paced_scales_and_caps_gaps():
    sleeps = []
    lines = ["D 10:00:00.0 a", "D 10:00:01.0 b", "D 10:00:01.0 c", "D 10:10:00.0 d"]
    list(paced(lines, 2.0, sleep=sleeps.append))
    assert sleeps == [0.5, MAX_SLEEP]


def test_paced_ignores_midnight_rollover():
    sleeps = []
    list(paced(["D 23:59:59.0 a", "D 00:00:01.0 b"], 1.0, sleep=sleeps.append))
    assert sleeps == []


def test_paced_replay_flushes_before_a_long_wait(mini_lines):
    from helpers import TEST_POOL

    from lscs.pipeline import Pipeline
    from lscs.replay import push_paced
    from lscs.state.models import EventType
    from lscs.state.tracker import StateTracker

    starts = []
    pipeline = Pipeline(
        StateTracker(TEST_POOL),
        lambda s, e: starts.extend(x.turn for x in e if x.type == EventType.RECRUIT_STARTED),
    )
    waits = []
    # 招募开始后等选项时如果要等很久（玩家在想），先把招募开始发出去
    push_paced(
        pipeline, mini_lines, 1.0, sleep=lambda s: waits.append((s, list(starts))), interval=0.2
    )
    # 10:00:05 → 10:00:10（招募开始）→ 10:00:20（买牌）：第三次等待之前招募开始已经发出
    assert [seen for _, seen in waits][:3] == [[], [], [1]]
    assert starts == [1, 2]


def test_push_paced_stops_when_asked(mini_lines):
    from helpers import TEST_POOL

    from lscs.pipeline import Pipeline
    from lscs.replay import push_paced
    from lscs.state.tracker import StateTracker

    updates = []
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: updates.append(s))
    push_paced(pipeline, mini_lines, None, sleep=lambda s: None, interval=0.2, stop=lambda: True)
    assert updates == []
