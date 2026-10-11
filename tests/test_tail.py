from lscs.logwatch.tail import LogTailer


def test_returns_only_new_complete_lines(tmp_path):
    log = tmp_path / "Power.log"
    log.write_bytes(b"a\nb\n")
    tailer = LogTailer(lambda: log)
    assert tailer.poll() == ["a", "b"]
    assert tailer.poll() == []
    with log.open("ab") as f:
        f.write(b"c\n")
    assert tailer.poll() == ["c"]


def test_partial_line_is_held_until_newline(tmp_path):
    log = tmp_path / "Power.log"
    log.write_bytes(b"first\nsec")
    tailer = LogTailer(lambda: log)
    assert tailer.poll() == ["first"]
    with log.open("ab") as f:
        f.write(b"ond\r\n")
    assert tailer.poll() == ["second"]


def test_switches_to_new_file_and_reads_from_start(tmp_path):
    first = tmp_path / "s1" / "Power.log"
    second = tmp_path / "s2" / "Power.log"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"old\n")
    current = [first]
    tailer = LogTailer(lambda: current[0])
    assert tailer.poll() == ["old"]
    second.write_bytes(b"new1\nnew2\n")
    current[0] = second
    assert tailer.poll() == ["new1", "new2"]
    assert tailer.path == second


def test_truncated_file_is_reread(tmp_path):
    log = tmp_path / "Power.log"
    log.write_bytes(b"long line one\n")
    tailer = LogTailer(lambda: log)
    tailer.poll()
    log.write_bytes(b"x\n")
    assert tailer.poll() == ["x"]


def test_missing_file_returns_nothing(tmp_path):
    assert LogTailer(lambda: None).poll() == []
    assert LogTailer(lambda: tmp_path / "nope.log").poll() == []


def test_invalid_utf8_is_replaced(tmp_path):
    log = tmp_path / "Power.log"
    log.write_bytes(b"ok \xff\n")
    assert LogTailer(lambda: log).poll() == ["ok �"]
