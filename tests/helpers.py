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


def _real_cards_path():
    from lscs.cards.fetch import CACHE_NAME, default_cache_dir

    return default_cache_dir() / CACHE_NAME


# 本机缓存的完整卡牌数据（lscs 第一次运行时下载）；没有时跳过依赖它的测试
REAL_CARDS = _real_cards_path()
NEEDS_REAL_CARDS = pytest.mark.skipif(not REAL_CARDS.exists(), reason="没有缓存的卡牌数据")

STATS_DIR = FIXTURES / "stats"  # 裁剪过的 Firestone 统计（trim_stats.py 生成）
STATS_FILES = ("hero-stats.json", "trinket-stats.json", "card-stats.json")
# 统计夹具是 Firestone 的数据，不放进公开仓库（scripts/export_public.py）；没有时跳过依赖它的测试
NEEDS_STATS_FIXTURES = pytest.mark.skipif(
    not all((STATS_DIR / name).exists() for name in STATS_FILES),
    reason="没有本版本统计的夹具（公开版不带）",
)


def fixture_stats(cards: bool = True, **cfg):
    """夹具里的本版本统计（MetaStats）；cfg 是 [stats] 的设置（默认全部玩家、本补丁）。
    时间按 UTC 显示，结果和机器的时区无关。"""
    from datetime import UTC

    from lscs.config import StatsConfig
    from lscs.stats.model import load_meta_stats

    card = STATS_DIR / "card-stats.json" if cards else None
    return load_meta_stats(
        STATS_DIR / "hero-stats.json",
        STATS_DIR / "trinket-stats.json",
        card,
        StatsConfig(enabled=True, cards=cards, **cfg),
        tz=UTC,
    )


def keep_away_from_real_files(monkeypatch, home):
    """命令行默认读项目目录下的 config.toml、把复盘写进 reviews/、把 AI 调用记在 recordings/、
    把面板拖到的位置写进 overlay-position.json、把本版本统计、HSReplay 流派指南和对手阵容的卡图
    缓存在本机的缓存目录：都换到 home 下面。也不让测试碰网络：urllib 的 urlopen 一调用就报错。conftest 的 autouse 守卫（每个用例）
    和在模块级 fixture 里回放录像的测试（守卫还没生效时）都用它。"""
    import urllib.request

    import lscs.cli
    from lscs.overlay import position

    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", home / "config.toml")
    monkeypatch.setattr(lscs.cli, "REVIEW_BASE", home)
    monkeypatch.setattr(lscs.cli, "STATS_CACHE_DIR", home / "cache")
    monkeypatch.setattr(lscs.cli, "GUIDES_CACHE_DIR", home / "cache")
    monkeypatch.setattr(lscs.cli, "ART_CACHE_DIR", home / "cache" / "art")
    monkeypatch.setattr(lscs.cli, "RECORDINGS_DIR", home / "recordings")
    monkeypatch.setattr(position, "DEFAULT_PATH", home / "overlay-position.json")

    def no_network(*_args, **_kwargs):
        raise OSError("测试不碰网络")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)


GUIDES_DIR = FIXTURES / "guides"  # 裁剪过的 HSReplay 流派指南和用到的卡（trim_guides.py 生成）
GUIDES_FILE = GUIDES_DIR / "hsreplay-comp-guides.json"
GUIDES_CARDS = GUIDES_DIR / "cards_guides.json"
# HSReplay 的样本没有授权，不放进公开仓库（scripts/export_public.py）；没有时跳过依赖它的测试
NEEDS_GUIDES_FIXTURES = pytest.mark.skipif(
    not (GUIDES_FILE.exists() and GUIDES_CARDS.exists()),
    reason="没有流派指南的夹具（公开版不带）",
)


def guides_cards():
    """夹具里的卡牌数据（指南引用到的卡和用户那局商店里的几张）。"""
    from lscs.cards.db import CardDB

    return CardDB.from_json_file(GUIDES_CARDS)


def fixture_live_guides(patch="2026-10-01"):
    """夹具里的 HSReplay 流派指南（LiveGuides）；时间按 UTC。"""
    from datetime import UTC, date

    from lscs.guides.model import load_live_guides

    return load_live_guides(GUIDES_FILE, date.fromisoformat(patch), guides_cards(), tz=UTC)
