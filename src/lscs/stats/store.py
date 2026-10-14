"""在后台准备本版本统计（本版本统计设计文档第 5.4 节）。

watch 在后台线程里下载（从不阻塞；统计好之前出现的选择按没有统计处理，不回头重画），
replay 和 review 同步执行（结果确定、便于对照）。任何出错都只报告一行、记日志，get() 一直是
None：统计失效时程序照常工作，只是没有统计。
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime, tzinfo
from pathlib import Path

from lscs.config import StatsConfig
from lscs.stats.fetch import MAX_AGE_SECONDS, Kind, Opener, _open, ensure_stats_json
from lscs.stats.model import MMR_ZH, PERIOD_ZH, MetaStats, load_meta_stats

log = logging.getLogger(__name__)

HOUR = 3600
DAY = 24 * HOUR


def _print(line: str) -> None:
    """后台线程也会打印：一次写完整行（带换行），不和主线程的输出交错在一行里。"""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="lscs-stats", daemon=True).start()


def start_text(cfg: StatsConfig, sync: bool) -> str:
    """启动时（cli 在主线程里）打印的一行。"""
    where = "" if sync else "后台"
    return (
        f"版本统计：开启（数据来自 Firestone，{MMR_ZH[cfg.mmr_percentile]}，"
        f"{PERIOD_ZH[cfg.period]}），正在{where}准备…"
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


def ready_text(stats: MetaStats) -> str:
    """准备好之后的一行；用的是旧缓存时写缓存的年龄。"""
    if stats.cached_age is not None:
        return f"版本统计：下载失败，使用{_cache_text(stats.cached_age)}（{stats.date_text} 更新）"
    counts = f"{len(stats.heroes)} 个英雄、{len(stats.trinkets)} 个饰品"
    if stats.cards:
        counts += f"、{len(stats.cards)} 张牌"
    return (
        f"版本统计：已就绪（{stats.local_updated:%m-%d %H:%M} 更新，"
        f"{stats.period_text} {stats.games} 局，{counts}）"
    )


class StatsStore:
    def __init__(
        self,
        cfg: StatsConfig,
        cache_dir: Path,
        *,
        opener: Opener = _open,
        now: Callable[[], float] = time.time,
        run_async: Callable[[Callable[[], None]], None] = _run_in_thread,
        report: Callable[[str], None] = _print,
        max_age: float = MAX_AGE_SECONDS,
        tz: tzinfo | None = None,
    ) -> None:
        """tz：显示时间用的时区；None 表示本机的时区（准备时取）。"""
        self._cfg = cfg
        self._tz = tz
        self._cache_dir = cache_dir
        self._opener = opener
        self._now = now
        self._run_async = run_async
        self._report = report
        self._max_age = max_age
        self._lock = threading.Lock()
        self._stats: MetaStats | None = None

    def start(self, sync: bool = False) -> None:
        """watch：后台线程；replay / review：同步。"""
        if sync:
            self._load()
        else:
            self._run_async(self._load)

    def get(self) -> MetaStats | None:
        """还没好、失败时为 None。"""
        with self._lock:
            return self._stats

    def _fetch(self, kind: Kind) -> tuple[Path, float | None]:
        """(缓存文件, 下载失败、用的是过期缓存时它的年龄)。"""
        cfg = self._cfg
        path = ensure_stats_json(
            self._cache_dir,
            kind,
            cfg.mmr_percentile,
            cfg.period,
            opener=self._opener,
            now=self._now,
            max_age=self._max_age,
        )
        age = self._now() - path.stat().st_mtime
        return path, (age if age >= self._max_age else None)

    def _load(self) -> None:
        # 三个文件串行：英雄 → 饰品 → 随从（[stats] cards = false 时不下载随从）。英雄、饰品
        # 下载失败就没有统计；只有随从失败时照样用英雄和饰品的
        try:
            fetched = [self._fetch("hero"), self._fetch("trinket")]
        except Exception as exc:
            log.warning("下载本版本统计失败", exc_info=True)
            self._report(f"版本统计：下载失败（{_reason(exc)}），本次没有统计")
            return
        card_path, card_error = None, None
        if self._cfg.cards:
            try:
                card = self._fetch("card")
            except Exception as exc:
                log.warning("下载随从统计失败，只用英雄和饰品数据", exc_info=True)
                card_error = _reason(exc)
            else:
                fetched.append(card)
                card_path = card[0]
        tz = self._tz if self._tz is not None else datetime.now().astimezone().tzinfo
        try:
            stats = load_meta_stats(fetched[0][0], fetched[1][0], card_path, self._cfg, tz=tz)
        except Exception as exc:
            log.warning("读取本版本统计失败", exc_info=True)
            self._report(f"版本统计：读取失败（{_reason(exc)}），本次没有统计")
            return
        ages = [age for _, age in fetched if age is not None]
        if ages:
            stats = stats.model_copy(update={"cached_age": max(ages)})
        with self._lock:
            self._stats = stats
        self._report(ready_text(stats))
        if card_error is not None:
            self._report(f"版本统计：随从统计没下载成功（{card_error}），只用英雄和饰品数据")
