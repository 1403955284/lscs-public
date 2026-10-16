"""在后台准备 HSReplay 的流派指南（流派指南自动更新设计文档第 3.4、3.6 节）。

watch 在后台线程里下载（从不阻塞；准备好之前的局面用 comps.md），replay 和 review 同步执行（结果
确定、便于对照）。本补丁更新过的条目不少于 MIN_FRESH 条时 comps() 是 HSReplay 的指南（comps.md
的通用运营段接在最后），否则就是 comps.md。任何出错都只报告一行、记日志，comps() 退回 comps.md：
指南失效时程序照常工作。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, tzinfo
from pathlib import Path

from lscs.cards.db import CardDB
from lscs.config import GuidesConfig
from lscs.guides.fetch import MAX_AGE_SECONDS, Opener, _open, ensure_guides_json
from lscs.guides.model import SOURCE, LiveGuides, load_live_guides, to_comps_file
from lscs.llm.comps import CompsFile

log = logging.getLogger(__name__)

MIN_FRESH = 5  # 本补丁更新过的指南少于这么多条（补丁刚换的几天）时用 comps.md
REMIND_DAYS = 25  # 最新的指南比 patch_date 晚这么多天以上：多半是换了补丁忘了改配置
# 今天离 patch_date 这么多天以上时启动时提醒一句：新扩展包上线头几天 HSReplay 还没更新，按最新的
# 指南看不出换了补丁，旧指南会一直被当作新鲜的
HINT_DAYS = 20
HOUR = 3600
DAY = 24 * HOUR


def _print(line: str) -> None:
    """后台线程也会打印：一次写完整行（带换行），不和主线程的输出交错在一行里。"""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="lscs-guides", daemon=True).start()


def start_text(cfg: GuidesConfig, sync: bool) -> str:
    """启动时（cli 在主线程里）打印的一行。"""
    where = "" if sync else "后台"
    return (
        f"流派指南：{SOURCE} 自动更新开启（数据来自 {SOURCE}，本补丁从 {cfg.patch_date} 起），"
        f"正在{where}准备…"
    )


def patch_hint(cfg: GuidesConfig, today: date) -> str | None:
    """今天离 patch_date 已经 HINT_DAYS 天以上时的一句软提醒；不到时 None。"""
    days = (today - cfg.patch_day).days
    if days < HINT_DAYS:
        return None
    return (
        f"流派指南：按 patch_date = {cfg.patch_date} 判断新旧（已经过了 {days} 天）；"
        "如果游戏已经换了大补丁，记得把 config.toml 里 [guides] patch_date 改成新补丁的日期"
    )


def _cache_text(seconds: float) -> str:
    """ "2 天前的缓存"；不到 1 小时（只在 max_age 改小时才会有）写"旧的缓存"。"""
    if seconds >= DAY:
        return f" {int(seconds // DAY)} 天前的缓存"
    if seconds >= HOUR:
        return f" {int(seconds // HOUR)} 小时前的缓存"
    return "旧的缓存"


def _reason(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


def _manual_text(manual: CompsFile | None) -> str:
    return f"comps.md 的 {manual.comp_count} 个流派" if manual is not None else ""


def ready_lines(live: LiveGuides, manual: CompsFile | None) -> list[str]:
    """准备好之后的一两行：用旧缓存时先写缓存的年龄；够数时"N 条可用"，不够时"只有 N 条"；最新的
    指南比 patch_date 晚 REMIND_DAYS 天以上时再提醒改配置。"""
    lines = []
    newest = f"{live.newest:%m-%d}"
    if live.cached_age is not None:
        lines.append(f"流派指南：下载失败，使用{_cache_text(live.cached_age)}（最新 {newest}）")
    count = len(live.guides)
    if count >= MIN_FRESH:
        line = (
            f"流派指南：{SOURCE} {count} 条可用（全部 {live.total} 条，"
            f"{live.stale} 条是补丁前的；最新 {newest}）"
        )
        if manual is not None:
            line += f"；{_manual_text(manual)}只作备用，通用运营照发"
    else:
        fallback = f"用 {_manual_text(manual)}" if manual is not None else " AI 只有通用攻略"
        line = (
            f"流派指南：{SOURCE} 本补丁更新过的只有 {count} 条（少于 {MIN_FRESH} 条），"
            f"这段时间{fallback}"
        )
    lines.append(line)
    if (live.newest - live.patch_date).days > REMIND_DAYS:
        lines.append(
            f"注意：{SOURCE} 最新的指南是 {newest} 更新的，[guides] patch_date 还是 "
            f"{live.patch_date.isoformat()}；换了补丁记得改它"
        )
    return lines


class GuidesStore:
    def __init__(
        self,
        cfg: GuidesConfig,
        cache_dir: Path,
        cards: CardDB,
        manual: CompsFile | None,
        *,
        opener: Opener = _open,
        now: Callable[[], float] = time.time,
        run_async: Callable[[Callable[[], None]], None] = _run_in_thread,
        report: Callable[[str], None] = _print,
        max_age: float = MAX_AGE_SECONDS,
        tz: tzinfo | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        """manual：comps.md（没有文件时 None）；HSReplay 的指南不够或失败时用它，它的通用运营段
        接在 HSReplay 的指南后面。tz：显示时间用的时区；None 表示本机的时区（准备时取）。
        today：判断"离 patch_date 多久"用的日期；None 时按 now 换算（测试可以单独固定它）。"""
        self._cfg = cfg
        self._cache_dir = cache_dir
        self._cards = cards
        self._manual = manual
        self._opener = opener
        self._now = now
        self._run_async = run_async
        self._report = report
        self._max_age = max_age
        self._tz = tz
        self._today = today
        self._lock = threading.Lock()
        self._live: LiveGuides | None = None
        self._comps: CompsFile | None = None  # HSReplay 的指南够数时转好的

    def start(self, sync: bool = False) -> None:
        """watch：后台线程；replay / review：同步。今天离 patch_date 太久时先提醒一句
        （只这一次）。"""
        tz = self._tz if self._tz is not None else datetime.now().astimezone().tzinfo
        day = self._today() if self._today else datetime.fromtimestamp(self._now(), tz).date()
        hint = patch_hint(self._cfg, day)
        if hint is not None:
            self._report(hint)
        if sync:
            self._load()
        else:
            self._run_async(self._load)

    def live(self) -> LiveGuides | None:
        """还没好、失败时为 None。"""
        with self._lock:
            return self._live

    def comps(self) -> CompsFile | None:
        """新鲜的指南不少于 MIN_FRESH 条时是 HSReplay 的，否则（还没好、失败、太少）是 comps.md。"""
        with self._lock:
            return self._comps if self._comps is not None else self._manual

    def _load(self) -> None:
        try:
            path = ensure_guides_json(
                self._cache_dir, opener=self._opener, now=self._now, max_age=self._max_age
            )
            age = self._now() - path.stat().st_mtime
        except Exception as exc:
            log.warning("下载流派指南失败", exc_info=True)
            self._report(f"流派指南：下载失败（{_reason(exc)}），这次用 comps.md")
            return
        tz = self._tz if self._tz is not None else datetime.now().astimezone().tzinfo
        try:
            live = load_live_guides(path, self._cfg.patch_day, self._cards, tz=tz)
            if age >= self._max_age:
                live = replace(live, cached_age=age)
            comps = to_comps_file(live, self._manual) if len(live.guides) >= MIN_FRESH else None
        except Exception as exc:
            log.warning("读取流派指南失败", exc_info=True)
            self._report(f"流派指南：读取失败（{_reason(exc)}），这次用 comps.md")
            return
        with self._lock:
            self._live = live
            self._comps = comps
        for line in ready_lines(live, self._manual):
            self._report(line)
