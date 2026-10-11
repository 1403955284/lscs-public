import json
from datetime import datetime

from lscs.llm.record import Recorder


def test_appends_one_json_line_per_call(tmp_path):
    recorder = Recorder(tmp_path / "recordings", now=lambda: datetime(2026, 10, 11, 20, 5, 9))
    recorder.write({"turn": 3, "outcome": "合法"})
    recorder.write({"turn": 4, "outcome": "无效：x"})
    lines = (
        (tmp_path / "recordings" / "llm-2026-10-11.jsonl").read_text(encoding="utf-8").splitlines()
    )
    assert [json.loads(line) for line in lines] == [
        {"time": "2026-10-11T20:05:09", "turn": 3, "outcome": "合法"},
        {"time": "2026-10-11T20:05:09", "turn": 4, "outcome": "无效：x"},
    ]


def test_write_failure_is_swallowed(tmp_path):
    blocker = tmp_path / "recordings"
    blocker.write_text("不是目录")
    Recorder(blocker).write({"turn": 1})  # 不抛异常


def test_concurrent_writes_keep_lines_whole(tmp_path):
    import threading

    recorder = Recorder(tmp_path, now=lambda: datetime(2026, 10, 11, 20, 5, 9))
    text = "局面" * 2000

    def write_many(n):
        for i in range(30):
            recorder.write({"worker": n, "i": i, "user": text})

    threads = [threading.Thread(target=write_many, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "llm-2026-10-11.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 120
    assert all(json.loads(line)["user"] == text for line in lines)
