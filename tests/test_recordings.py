"""你自己录制的对局：时间线必须和人工确认过的快照一致。

新增录像的步骤见 README 的「添加录像」一节。没有录像时，这个测试自动跳过。
"""

import pytest
from helpers import FIXTURES

from lscs.cli import DEFAULT_POOL
from lscs.dump import describe
from lscs.replay import read_log_lines
from lscs.state.pool import load_pool_config

RECORDINGS = sorted((FIXTURES / "recordings").glob("*.log.gz"))


@pytest.mark.parametrize("log_path", RECORDINGS, ids=lambda p: p.name)
def test_timeline_matches_snapshot(log_path, run_log):
    snapshot = log_path.with_name(log_path.name.removesuffix(".log.gz") + ".timeline.txt")
    lines = []
    for state, events in run_log(read_log_lines(log_path), pool=load_pool_config(DEFAULT_POOL)):
        lines.extend(describe(state, events))
    assert "\n".join(lines) + "\n" == snapshot.read_text(encoding="utf-8")
