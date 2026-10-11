"""把日志行串成状态更新：同一时间戳的行算一组，组结束时 flush。

例外：招募开始那一组之后，本回合的第一组选项（英雄技能能不能用）隔几十毫秒才到。
这时先不 flush：只要后面来的都是选项（可能跨好几个时间戳），就一直等；
来了别的局面变化，就先 flush 再应用它。watch 读不到新行时照常 flush。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from lscs.parsing.feed import OptionListed, OptionsStarted, PowerChange, PowerFeed
from lscs.state.models import Event, GameState
from lscs.state.tracker import StateTracker

UpdateHandler = Callable[[GameState, list[Event]], None]

log = logging.getLogger(__name__)


def _only_options(changes: list[PowerChange]) -> bool:
    return all(isinstance(c, OptionsStarted | OptionListed) for c in changes)


def _timestamp(line: str) -> str | None:
    parts = line.split(" ", 2)
    return parts[1] if len(parts) == 3 else None


class Pipeline:
    def __init__(self, tracker: StateTracker, on_update: UpdateHandler) -> None:
        self._feed = PowerFeed()
        self._tracker = tracker
        self._on_update = on_update
        self._last_ts: str | None = None
        self._holding = False  # 招募开始那一组已经结束，为了等选项还没 flush
        self._errors = 0
        self._flush_error: tuple[str, str] | None = None  # 上次 flush 出错的类型和信息

    def push(self, line: str) -> None:
        ts = _timestamp(line)
        if ts is not None and ts != self._last_ts:
            if self._tracker.awaiting_options:
                self._holding = True
            elif not self._holding:
                self.flush()
            self._last_ts = ts
        try:
            changes = self._feed.feed(line)
            if self._holding and not _only_options(changes):
                self.flush()  # 局面变了：先发出招募开始（和已经到了的选项），不再等
            self._tracker.apply(changes)
        except Exception:
            log.warning("处理日志行时出错，状态可能不准", exc_info=True)
            self._tracker.mark_uncertain()
        if self._feed.errors != self._errors:
            self._errors = self._feed.errors
            self._tracker.mark_uncertain()

    def flush(self) -> None:
        self._holding = False
        try:
            result = self._tracker.flush()
        except Exception as exc:
            # watch 每次读不到新行都会 flush；同一个错误只打印一次完整的堆栈，免得刷屏
            key = (type(exc).__name__, str(exc))
            if key != self._flush_error:
                self._flush_error = key
                log.warning("生成状态快照时出错，状态可能不准", exc_info=True)
            else:
                log.debug("生成状态快照时又出错：%s", exc)
            self._tracker.mark_uncertain()
            return
        self._flush_error = None
        if result is None:
            return
        try:
            self._on_update(*result)
        except Exception:
            log.warning("输出状态更新时出错", exc_info=True)
