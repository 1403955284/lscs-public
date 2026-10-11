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
