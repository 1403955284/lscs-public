"""把每次大模型调用追加写入 recordings/llm-<日期>.jsonl（不包含 Key）。"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)


class Recorder:
    def __init__(self, directory: Path, now: Callable[[], datetime] = datetime.now) -> None:
        self._dir = directory
        self._now = now
        self._lock = threading.Lock()  # 被取代的请求和新请求可能同时在后台线程里写

    def write(self, entry: dict) -> None:
        moment = self._now()
        path = self._dir / f"llm-{moment:%Y-%m-%d}.jsonl"
        line = json.dumps(
            {"time": moment.isoformat(timespec="seconds"), **entry}, ensure_ascii=False
        )
        try:
            with self._lock:
                self._dir.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            log.warning("写入调用记录失败", exc_info=True)
