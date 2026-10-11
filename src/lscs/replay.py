"""读取录像日志并按原始时间节奏回放。"""

from __future__ import annotations

import gzip
import re
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lscs.pipeline import Pipeline

_TIMESTAMP = re.compile(r"^[DWE] (\d+):(\d+):(\d+(?:\.\d+)?) ")
MAX_SLEEP = 2.0  # 单次最多等待 2 秒，跳过大厅等待之类的长空档


def read_log_lines(path: Path) -> Iterator[str]:
    """逐行读取日志（支持 .gz），去掉行尾换行符。"""
    if path.suffix == ".gz":
        f = gzip.open(path, "rt", encoding="utf-8", errors="replace")
    else:
        f = path.open("r", encoding="utf-8", errors="replace")
    with f:
        for line in f:
            yield line.rstrip("\r\n")


def line_seconds(line: str) -> float | None:
    m = _TIMESTAMP.match(line)
    if not m:
        return None
    hours, minutes, seconds = m.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def paced(
    lines: Iterable[str], speed: float | None, sleep: Callable[[float], None] = time.sleep
) -> Iterator[str]:
    """按日志时间戳的节奏输出行；speed 为 None 时不等待。"""
    last: float | None = None
    for line in lines:
        if speed:
            t = line_seconds(line)
            if t is not None:
                if last is not None and t > last:
                    sleep(min((t - last) / speed, MAX_SLEEP))
                last = t
        yield line


def push_paced(
    pipeline: Pipeline,
    lines: Iterable[str],
    speed: float | None,
    *,
    sleep: Callable[[float], None],
    interval: float,
    stop: Callable[[], bool] | None = None,
) -> None:
    """按节奏把行推给 pipeline，最后 flush。要等得比 watch 的轮询间隔久时先 flush，
    和 watch 读不到新行时一样：不然等选项的招募开始要拖到玩家下一次操作才显示。"""

    def wait(seconds: float) -> None:
        if seconds > interval:
            pipeline.flush()
        sleep(seconds)

    for line in paced(lines, speed, sleep=wait):
        if stop is not None and stop():
            break
        pipeline.push(line)
    pipeline.flush()
