from pathlib import Path

import pytest

from lscs.state.pool import PoolConfig

FIXTURES = Path(__file__).parent / "fixtures"
MINI_LOG = FIXTURES / "power" / "mini_bg.log"
REAL_LOG = FIXTURES / "power" / "bg_139963.log.gz"
TEST_POOL = PoolConfig(tribe_count=5, copies={1: 16, 2: 15})
USER_LOG = FIXTURES / "recordings" / "2026-10-11_cariel_3rd.log.gz"
# 用户自己的录像带有战网昵称，不放进公开仓库；没有这个文件时跳过依赖它的测试
NEEDS_USER_LOG = pytest.mark.skipif(not USER_LOG.exists(), reason="没有用户录像")
HERO_LOG = FIXTURES / "power" / "hero_select.log"  # 选英雄：开局 4 个、重随 1 个、选择、第 1 回合
CC0_POOL = PoolConfig(tribe_count=5, copies={1: 16, 2: 15, 3: 13, 4: 11, 5: 9, 6: 7})
_REPLAYS: dict = {}


def replay_log(path, pool):
    """把整份日志推过 Pipeline，返回 [(state, events), ...]；同一份日志、同样的卡池配置只回放一次。"""
    from lscs.pipeline import Pipeline
    from lscs.replay import read_log_lines
    from lscs.state.tracker import StateTracker

    key = (path, pool.tribe_count, tuple(sorted(pool.copies.items())))
    if key not in _REPLAYS:
        updates = []
        pipeline = Pipeline(StateTracker(pool), lambda s, e: updates.append((s, e)))
        for line in read_log_lines(path):
            pipeline.push(line)
        pipeline.flush()
        _REPLAYS[key] = updates
    return _REPLAYS[key]


def recruit_at(updates, turn):
    """第 turn 回合招募开始时的快照。"""
    from lscs.state.models import EventType

    for state, events in updates:
        if any(e.type == EventType.RECRUIT_STARTED and e.turn == turn for e in events):
            return state
    raise AssertionError(f"没有第 {turn} 回合的招募开始")


def combat_at(updates, turn):
    """第 turn 回合战斗中的第一个快照。"""
    from lscs.state.models import Phase

    return next(s for s, _ in updates if s.phase == Phase.COMBAT and s.turn == turn)
