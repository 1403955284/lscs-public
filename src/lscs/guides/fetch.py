"""下载并缓存 HSReplay 的流派指南（流派指南自动更新设计文档第 3.1 节）。

照 stats/fetch.py 的做法：只有一个文件，每天最多下载一次，缓存在卡牌数据的缓存目录；下载失败时
沿用旧缓存。响应可能是 gzip 压缩的（正文以 1f 8b 开头），缓存里存解压后的明文。内容不是"每个元素
都带 name（字符串）和 core_cards（列表）的 JSON 列表"时不缓存（防止把登录页缓存下来）。
"""

from __future__ import annotations

import gzip
import http.client
import json
import logging
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

from lscs.cards.fetch import USER_AGENT

log = logging.getLogger(__name__)

GUIDES_URL = "https://hsreplay.net/api/v1/battlegrounds/comp_guides/"
CACHE_NAME = "hsreplay-comp-guides.json"
MAX_AGE_SECONDS = 24 * 3600
TIMEOUT_SECONDS = 30
GZIP_MAGIC = b"\x1f\x8b"
BAD_FORMAT = "流派指南格式不对"
Opener = Callable[[urllib.request.Request], BinaryIO]


def _open(request: urllib.request.Request) -> BinaryIO:
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def _valid(parsed: object) -> bool:
    return isinstance(parsed, list) and all(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("core_cards"), list)
        for item in parsed
    )


def _download(opener: Opener) -> bytes:
    """下载、解压，确认格式；返回明文。"""
    request = urllib.request.Request(
        GUIDES_URL,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    )
    with opener(request) as response:
        data = response.read()
    # 只看正文开头：响应头说是 gzip、正文却已经解压过（代理）时不能再解一次
    if data[:2] == GZIP_MAGIC:
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise ValueError(BAD_FORMAT) from exc
    try:
        parsed = json.loads(data)
    except ValueError as exc:  # 包括 UnicodeDecodeError
        raise ValueError(BAD_FORMAT) from exc
    if not _valid(parsed):
        raise ValueError(BAD_FORMAT)
    return data


def ensure_guides_json(
    cache_dir: Path,
    *,
    opener: Opener = _open,
    now: Callable[[], float] = time.time,
    max_age: float = MAX_AGE_SECONDS,
) -> Path:
    """返回本地缓存的流派指南；缓存过期时重新下载，下载失败则沿用旧缓存（没有旧缓存时抛出）。"""
    path = cache_dir / CACHE_NAME
    if path.exists() and now() - path.stat().st_mtime < max_age:
        return path
    try:
        data = _download(opener)
    except (OSError, http.client.HTTPException, ValueError):
        if path.exists():
            log.warning("下载流派指南失败，继续使用旧缓存 %s", path, exc_info=True)
            return path
        raise
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path
