"""后台加载本版本统计（本版本统计设计文档第 5.4 节）：run_async 注入成同步执行，网络用 FakeOpener。"""

import io
import os
import time
from datetime import UTC

import pytest
from helpers import NEEDS_STATS_FIXTURES, STATS_DIR

from lscs.config import StatsConfig
from lscs.stats.fetch import cache_name, stats_url
from lscs.stats.store import StatsStore, start_text

pytestmark = NEEDS_STATS_FIXTURES  # 公开版没有统计夹具：整个模块跳过

STATS = STATS_DIR
FILES = {
    stats_url("hero", 100, "last-patch"): "hero-stats.json",
    stats_url("trinket", 100, "last-patch"): "trinket-stats.json",
    stats_url("card", 100, "last-patch"): "card-stats.json",
}
BODIES: dict[str, bytes] = {}  # 地址 → 夹具内容（第一次用到时由 _bodies 读进来）
ON = StatsConfig(enabled=True)


@pytest.fixture(autouse=True)
def _bodies():
    """测试运行时才读夹具：收集时不读文件，公开版没有夹具时跳过而不是报错。"""
    if not BODIES:
        BODIES.update({url: (STATS / name).read_bytes() for url, name in FILES.items()})


class FakeOpener:
    def __init__(self, error=None, only=None):
        """only：只在这个地址上出错。"""
        self.error, self.only, self.urls = error, only, []

    def __call__(self, request):
        self.urls.append(request.full_url)
        if self.error and self.only in (None, request.full_url):
            raise self.error
        return io.BytesIO(BODIES[request.full_url])


def make_store(tmp_path, cfg=ON, opener=None, **kwargs):
    lines, jobs = [], []
    kwargs.setdefault("tz", UTC)  # 时间按 UTC 显示，和机器的时区无关
    store = StatsStore(
        cfg,
        tmp_path / "cache",
        opener=opener or FakeOpener(),
        run_async=jobs.append,
        report=lines.append,
        **kwargs,
    )
    return store, lines, jobs


def test_start_text():
    assert start_text(ON, sync=False) == (
        "版本统计：开启（数据来自 Firestone，全部玩家，本补丁），正在后台准备…"
    )
    week = StatsConfig(enabled=True, mmr_percentile=50, period="past-seven")
    assert (
        start_text(week, sync=True)
        == "版本统计：开启（数据来自 Firestone，前 50%，最近 7 天），正在准备…"
    )


def test_loads_in_the_background_and_reports_ready(tmp_path):
    opener = FakeOpener()
    store, lines, jobs = make_store(tmp_path, opener=opener)
    store.start()
    assert store.get() is None and opener.urls == [] and len(jobs) == 1  # 还没跑
    jobs[0]()
    stats = store.get()
    assert stats is not None and stats.hero("BG21_HERO_000").average_position == 3.67
    assert stats.cached_age is None
    # 英雄 → 饰品 → 随从，串行
    assert opener.urls == list(BODIES)
    assert lines == [
        "版本统计：已就绪（10-14 00:10 更新，本补丁 938474 局，11 个英雄、10 个饰品、8 张牌）"
    ]
    assert (tmp_path / "cache" / cache_name("hero", 100, "last-patch")).exists()


def test_sync_start_skips_run_async(tmp_path):
    store, lines, jobs = make_store(tmp_path)
    store.start(sync=True)
    assert jobs == [] and store.get() is not None and len(lines) == 1


def test_cards_switched_off_skips_the_third_file(tmp_path):
    opener = FakeOpener()
    store, lines, _ = make_store(tmp_path, StatsConfig(enabled=True, cards=False), opener)
    store.start(sync=True)
    assert opener.urls == list(BODIES)[:2]
    assert store.get().cards == {}
    assert lines == ["版本统计：已就绪（10-14 00:10 更新，本补丁 938474 局，11 个英雄、10 个饰品）"]


def test_failure_without_cache_reports_once_and_never_raises(tmp_path):
    store, lines, _ = make_store(tmp_path, opener=FakeOpener(OSError("网络不通")))
    store.start(sync=True)
    assert store.get() is None
    assert lines == ["版本统计：下载失败（网络不通），本次没有统计"]


def test_unreadable_cache_reports_a_read_failure(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    for kind in ("hero", "trinket", "card"):
        (cache / cache_name(kind, 100, "last-patch")).write_text("{}", encoding="utf-8")
    store, lines, _ = make_store(tmp_path)
    store.start(sync=True)  # 缓存是新的，不下载；但内容不对
    assert store.get() is None
    assert len(lines) == 1 and lines[0].startswith("版本统计：读取失败（")
    assert lines[0].endswith("），本次没有统计")


def seed_stale_cache(tmp_path, age):
    cache = tmp_path / "cache"
    cache.mkdir()
    for kind, name in (("hero", "hero"), ("trinket", "trinket"), ("card", "card")):
        path = cache / cache_name(kind, 100, "last-patch")
        path.write_bytes((STATS / f"{name}-stats.json").read_bytes())
        os.utime(path, (time.time() - age, time.time() - age))


def test_stale_cache_is_used_when_the_download_fails(tmp_path):
    seed_stale_cache(tmp_path, 2 * 86400 + 600)
    store, lines, _ = make_store(tmp_path, opener=FakeOpener(OSError("offline")))
    store.start(sync=True)
    stats = store.get()
    assert stats is not None and stats.cached_age >= 2 * 86400
    assert lines == ["版本统计：下载失败，使用 2 天前的缓存（10-14 更新）"]


def test_stale_cache_hours(tmp_path):
    store, lines, _ = make_store(tmp_path, opener=FakeOpener(OSError("offline")), max_age=3600)
    seed_stale_cache(tmp_path, 5 * 3600 + 60)
    store.start(sync=True)
    assert lines == ["版本统计：下载失败，使用 5 小时前的缓存（10-14 更新）"]


def test_default_run_async_uses_a_daemon_thread(monkeypatch, tmp_path):
    import threading

    made = []

    class FakeThread:
        def __init__(self, target, name, daemon):
            made.append((name, daemon))
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(threading, "Thread", FakeThread)
    lines = []
    store = StatsStore(ON, tmp_path / "cache", opener=FakeOpener(), report=lines.append)
    store.start()
    assert made == [("lscs-stats", True)]
    assert store.get() is not None


def test_ready_line_uses_local_time(tmp_path):
    from datetime import timedelta, timezone

    store, lines, _ = make_store(tmp_path, tz=timezone(timedelta(hours=8)))
    store.start(sync=True)
    assert lines[0].startswith("版本统计：已就绪（10-14 08:10 更新，")
    assert store.get().source_text().endswith("· 10-14 更新")


def test_default_timezone_is_the_machines(tmp_path):
    from datetime import datetime

    store = StatsStore(
        ON, tmp_path / "cache", opener=FakeOpener(), run_async=None, report=lambda _line: None
    )
    store.start(sync=True)
    stats = store.get()
    assert stats.tz is not None
    assert stats.tz.utcoffset(None) == datetime.now().astimezone().utcoffset()


def test_card_download_failure_keeps_heroes_and_trinkets(tmp_path):
    card = stats_url("card", 100, "last-patch")
    store, lines, _ = make_store(tmp_path, opener=FakeOpener(OSError("网络不通"), only=card))
    store.start(sync=True)
    stats = store.get()
    assert stats is not None and stats.cards == {}
    assert stats.hero("BG21_HERO_000") is not None and stats.trinket("BG36_MagicItem_402")
    assert lines == [
        "版本统计：已就绪（10-14 00:10 更新，本补丁 938474 局，11 个英雄、10 个饰品）",
        "版本统计：随从统计没下载成功（网络不通），只用英雄和饰品数据",
    ]


def test_default_report_writes_each_line_in_one_call(monkeypatch):
    import sys

    from lscs.stats import store as store_module

    calls = []

    class Out:
        def write(self, text):
            calls.append(("write", text))

        def flush(self):
            calls.append(("flush",))

    monkeypatch.setattr(sys, "stdout", Out())
    store_module._print("版本统计：已就绪（……）")
    # 后台线程打印：一次写完整行，不和时间线的输出交错在一行里
    assert calls == [("write", "版本统计：已就绪（……）\n"), ("flush",)]
