"""后台加载 HSReplay 的流派指南（流派指南自动更新设计文档第 3.4、3.6 节）：run_async 注入成同步
执行，网络用 FakeOpener。"""

import io
import os
import time
from datetime import UTC, date

import pytest
from helpers import FIXTURES, GUIDES_FILE, NEEDS_GUIDES_FIXTURES, guides_cards

from lscs.config import GuidesConfig
from lscs.guides.fetch import CACHE_NAME, GUIDES_URL
from lscs.guides.store import MIN_FRESH, GuidesStore, start_text
from lscs.llm.comps import parse_comps
from lscs.llm.prompt import load_guide

pytestmark = NEEDS_GUIDES_FIXTURES  # 公开版没有指南夹具：整个模块跳过

ON = GuidesConfig(enabled=True)
BODY: list[bytes] = []


@pytest.fixture(autouse=True)
def _body():
    """测试运行时才读夹具：收集时不读文件，公开版没有夹具时跳过而不是报错。"""
    if not BODY:
        BODY.append(GUIDES_FILE.read_bytes())


def manual():
    return parse_comps(load_guide(FIXTURES / "comps_sample.md"))


class FakeOpener:
    def __init__(self, error=None):
        self.error, self.urls = error, []

    def __call__(self, request):
        self.urls.append(request.full_url)
        if self.error:
            raise self.error
        return io.BytesIO(BODY[0])


def make_store(tmp_path, cfg=ON, opener=None, comps="sample", **kwargs):
    lines, jobs = [], []
    kwargs.setdefault("tz", UTC)
    if "now" not in kwargs:  # 补丁提醒只看日期：固定在 patch_date 之后不久，测试不随真实日期变红
        kwargs.setdefault("today", lambda: date(2026, 10, 16))
    store = GuidesStore(
        cfg,
        tmp_path / "cache",
        guides_cards(),
        manual() if comps == "sample" else comps,
        opener=opener or FakeOpener(),
        run_async=jobs.append,
        report=lines.append,
        **kwargs,
    )
    return store, lines, jobs


def test_start_text():
    assert start_text(ON, sync=False) == (
        "流派指南：HSReplay 自动更新开启（数据来自 HSReplay，本补丁从 2026-10-01 起），"
        "正在后台准备…"
    )
    assert start_text(GuidesConfig(enabled=True, patch_date="2026-10-21"), sync=True) == (
        "流派指南：HSReplay 自动更新开启（数据来自 HSReplay，本补丁从 2026-10-21 起），正在准备…"
    )


def test_loads_in_the_background_and_replaces_the_comps(tmp_path):
    opener = FakeOpener()
    store, lines, jobs = make_store(tmp_path, opener=opener)
    store.start()
    # 还没好：用 comps.md
    assert store.live() is None and store.comps() == manual() and len(jobs) == 1
    jobs[0]()
    assert opener.urls == [GUIDES_URL]
    live = store.live()
    assert live is not None and len(live.guides) == 7 and live.cached_age is None
    comps = store.comps()
    assert comps.title.startswith("第 14 赛季流派指南（数据来自 HSReplay，10-15 更新，7 条")
    assert comps.guides[0].name == "中立·守门融合怪"
    assert comps.guides[-1].name == "通用运营（本赛季）"
    assert store.comps() is comps  # 只转一次
    assert lines == [
        "流派指南：HSReplay 7 条可用（全部 9 条，2 条是补丁前的；最新 10-15）；"
        "comps.md 的 4 个流派只作备用，通用运营照发"
    ]
    assert (tmp_path / "cache" / CACHE_NAME).exists()


def test_sync_start_skips_run_async(tmp_path):
    store, lines, jobs = make_store(tmp_path)
    store.start(sync=True)
    assert jobs == [] and store.live() is not None and len(lines) == 1


def test_too_few_fresh_guides_fall_back_to_comps_md(tmp_path):
    assert MIN_FRESH == 5
    store, lines, _ = make_store(tmp_path, GuidesConfig(enabled=True, patch_date="2026-10-14"))
    store.start(sync=True)
    assert len(store.live().guides) == 1
    assert store.comps() == manual()
    assert lines == [
        "流派指南：HSReplay 本补丁更新过的只有 1 条（少于 5 条），这段时间用 comps.md 的 4 个流派"
    ]


def test_too_few_and_no_comps_md(tmp_path):
    cfg = GuidesConfig(enabled=True, patch_date="2026-10-14")
    store, lines, _ = make_store(tmp_path, cfg, comps=None)
    store.start(sync=True)
    assert store.comps() is None
    assert lines == [
        "流派指南：HSReplay 本补丁更新过的只有 1 条（少于 5 条），这段时间 AI 只有通用攻略"
    ]


def test_ready_without_comps_md(tmp_path):
    store, lines, _ = make_store(tmp_path, comps=None)
    store.start(sync=True)
    assert store.comps().title.startswith("本赛季流派指南（数据来自 HSReplay，")
    assert lines == ["流派指南：HSReplay 7 条可用（全部 9 条，2 条是补丁前的；最新 10-15）"]


def test_failure_without_cache_reports_once_and_never_raises(tmp_path):
    store, lines, _ = make_store(tmp_path, opener=FakeOpener(OSError("网络不通")))
    store.start(sync=True)
    assert store.live() is None and store.comps() == manual()
    assert lines == ["流派指南：下载失败（网络不通），这次用 comps.md"]


def test_unreadable_cache_reports_a_read_failure(tmp_path):
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / CACHE_NAME).write_text("{}", encoding="utf-8")  # 新的缓存，不下载；但内容不对
    store, lines, _ = make_store(tmp_path)
    store.start(sync=True)
    assert store.live() is None and store.comps() == manual()
    assert len(lines) == 1 and lines[0].startswith("流派指南：读取失败（")
    assert lines[0].endswith("），这次用 comps.md")


def seed_stale_cache(tmp_path, age):
    cache = tmp_path / "cache"
    cache.mkdir()
    path = cache / CACHE_NAME
    path.write_bytes(BODY[0])
    os.utime(path, (time.time() - age, time.time() - age))


def test_stale_cache_is_used_when_the_download_fails(tmp_path):
    seed_stale_cache(tmp_path, 2 * 86400 + 600)
    store, lines, _ = make_store(tmp_path, opener=FakeOpener(OSError("offline")))
    store.start(sync=True)
    assert store.live().cached_age >= 2 * 86400
    assert store.comps().guides[0].name == "中立·守门融合怪"
    assert lines == [
        "流派指南：下载失败，使用 2 天前的缓存（最新 10-15）",
        "流派指南：HSReplay 7 条可用（全部 9 条，2 条是补丁前的；最新 10-15）；"
        "comps.md 的 4 个流派只作备用，通用运营照发",
    ]


def test_old_patch_date_gets_a_reminder(tmp_path):
    # 最新 10-15，patch_date 09-19：晚 26 天
    store, lines, _ = make_store(tmp_path, GuidesConfig(enabled=True, patch_date="2026-09-19"))
    store.start(sync=True)
    assert lines[-1] == (
        "注意：HSReplay 最新的指南是 10-15 更新的，[guides] patch_date 还是 2026-09-19；"
        "换了补丁记得改它"
    )
    store, lines, _ = make_store(tmp_path, GuidesConfig(enabled=True, patch_date="2026-09-20"))
    store.start(sync=True)
    assert not any(line.startswith("注意") for line in lines)  # 正好 25 天：不提醒


def test_errors_in_the_comps_conversion_fall_back(tmp_path, monkeypatch):
    import lscs.guides.store

    def boom(*_args):
        raise RuntimeError("坏了")

    monkeypatch.setattr(lscs.guides.store, "to_comps_file", boom)
    store, lines, _ = make_store(tmp_path)
    store.start(sync=True)
    assert store.comps() == manual()
    assert lines == ["流派指南：读取失败（坏了），这次用 comps.md"]


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
    store = GuidesStore(
        ON, tmp_path / "cache", guides_cards(), None, opener=FakeOpener(), report=lambda _: None
    )
    store.start()
    assert made == [("lscs-guides", True)]
    assert store.live() is not None


def _at(day):
    """那天中午（UTC）的时间戳。"""
    from datetime import datetime

    return datetime.fromisoformat(f"{day}T12:00:00+00:00").timestamp()


def _fresh_cache(tmp_path, when):
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    path = cache / CACHE_NAME
    path.write_bytes(BODY[0])
    os.utime(path, (when, when))


@pytest.mark.parametrize(("today", "hinted"), [("2026-10-20", False), ("2026-10-21", True)])
def test_a_long_time_after_the_patch_date_gets_a_soft_hint(tmp_path, today, hinted):
    from lscs.guides.store import HINT_DAYS, patch_hint

    assert HINT_DAYS == 20
    now = _at(today)
    _fresh_cache(tmp_path, now)  # 缓存是新的：不下载
    opener = FakeOpener()
    store, lines, _ = make_store(tmp_path, opener=opener, now=lambda: now)
    store.start(sync=True)
    assert opener.urls == []
    hint = (
        "流派指南：按 patch_date = 2026-10-01 判断新旧（已经过了 20 天）；"
        "如果游戏已经换了大补丁，记得把 config.toml 里 [guides] patch_date 改成新补丁的日期"
    )
    assert (lines[0] == hint) is hinted
    assert sum("判断新旧" in line for line in lines) == int(hinted)  # 只提醒一次
    assert lines[-1].startswith("流派指南：HSReplay 7 条可用")
    from datetime import date

    assert patch_hint(ON, date(2026, 10, 16)) is None


def test_both_reminders_can_show(tmp_path):
    # 今天 11-20、patch_date 09-19：两句都写（今天的软提醒在前，最新指南的提醒在最后）
    now = _at("2026-11-20")
    _fresh_cache(tmp_path, now)
    cfg = GuidesConfig(enabled=True, patch_date="2026-09-19")
    store, lines, _ = make_store(tmp_path, cfg, now=lambda: now)
    store.start(sync=True)
    assert lines[0].startswith("流派指南：按 patch_date = 2026-09-19 判断新旧（已经过了 62 天）")
    assert lines[-1].startswith("注意：HSReplay 最新的指南是 10-15 更新的")


def test_ready_line_counts_add_up(tmp_path):
    """ "N 条可用（全部 M 条，S 条是补丁前的…）"：N + S = M（隐藏的那条不算进"全部"）。"""
    import re

    store, lines, _ = make_store(tmp_path)
    store.start(sync=True)
    found = re.search(r"(\d+) 条可用（全部 (\d+) 条，(\d+) 条是补丁前的", lines[0])
    usable, total, stale = map(int, found.groups())
    assert (usable, total, stale) == (7, 9, 2) and usable + stale == total


def test_store_tests_do_not_depend_on_the_real_date(tmp_path, monkeypatch):
    """真实日期过了 patch_date 20 天以后，make_store 的测试也不会多出一行补丁提醒。"""
    monkeypatch.setattr(time, "time", lambda: _at("2026-12-25"))
    store, lines, _ = make_store(tmp_path)
    store.start(sync=True)
    assert not any("patch_date" in line for line in lines)
