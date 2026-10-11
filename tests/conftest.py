import pytest
from helpers import MINI_LOG, TEST_POOL

from lscs.pipeline import Pipeline
from lscs.replay import read_log_lines
from lscs.state.tracker import StateTracker


@pytest.fixture
def run_log():
    """把日志行全部推过 Pipeline，返回 [(state, events), ...]。"""

    def run(lines, pool=TEST_POOL):
        updates = []
        pipeline = Pipeline(StateTracker(pool), lambda s, e: updates.append((s, e)))
        for line in lines:
            pipeline.push(line)
        pipeline.flush()
        return updates

    return run


@pytest.fixture
def mini_lines() -> list[str]:
    return list(read_log_lines(MINI_LOG))
