"""轮询跟踪最新的 Power.log。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


class LogTailer:
    """每次 poll() 返回新增的完整行；切换到新文件或文件被截断时从头读起。"""

    def __init__(self, locate: Callable[[], Path | None]) -> None:
        self._locate = locate
        self._path: Path | None = None
        self._pos = 0
        self._partial = b""

    @property
    def path(self) -> Path | None:
        return self._path

    def poll(self) -> list[str]:
        path = self._locate()
        if path is None:
            return []
        if path != self._path:
            self._path, self._pos, self._partial = path, 0, b""
        try:
            if path.stat().st_size < self._pos:
                self._pos, self._partial = 0, b""
            with path.open("rb") as f:
                f.seek(self._pos)
                data = f.read()
                self._pos = f.tell()
        except FileNotFoundError:
            return []
        chunks = (self._partial + data).split(b"\n")
        self._partial = chunks.pop()
        return [c.decode("utf-8", errors="replace").rstrip("\r") for c in chunks]
