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
