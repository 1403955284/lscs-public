"""下载并缓存 Firestone 的统计文件（本版本统计设计文档第 4 节）。

照 cards/fetch.py 的做法：每个文件每天最多下载一次，缓存在卡牌数据的缓存目录；下载失败时沿用
旧缓存。响应可能是 gzip 压缩的（正文以 1f 8b 开头），缓存里存解压后的明文。内容不是带对应
顶层键的 JSON 对象时不缓存（防止把错误页缓存下来）。
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
from typing import BinaryIO, Literal

from lscs.cards.fetch import USER_AGENT

log = logging.getLogger(__name__)

Kind = Literal["hero", "trinket", "card"]
BASE_URL = "https://static.zerotoheroes.com/api/bgs/"
MAX_AGE_SECONDS = 24 * 3600
TIMEOUT_SECONDS = 30
TOP_KEYS: dict[Kind, str] = {"hero": "heroStats", "trinket": "trinketStats", "card": "cardStats"}
GZIP_MAGIC = b"\x1f\x8b"
Opener = Callable[[urllib.request.Request], BinaryIO]


def stats_url(kind: Kind, mmr: int, period: str) -> str:
    """饰品的路径没有分段（加 mmr-100/ 返回 403），分段写在每个条目里。"""
    if kind == "trinket":
        return f"{BASE_URL}trinket-stats/{period}/overview-from-hourly.gz.json"
    return f"{BASE_URL}{kind}-stats/mmr-{mmr}/{period}/overview-from-hourly.gz.json"


def cache_name(kind: Kind, mmr: int, period: str) -> str:
    if kind == "trinket":
        return f"firestone-trinket-{period}.json"
    return f"firestone-{kind}-mmr{mmr}-{period}.json"


def _open(request: urllib.request.Request) -> BinaryIO:
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def _download(url: str, kind: Kind, opener: Opener) -> bytes:
    """下载、解压，确认是带 TOP_KEYS[kind] 列表的 JSON 对象；返回明文。"""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    )
    with opener(request) as response:
        data = response.read()
    # 只看正文开头：响应头说是 gzip、正文却已经解压过（代理）时不能再解一次
    if data[:2] == GZIP_MAGIC:
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as exc:
            raise ValueError("统计数据格式不对") from exc
    try:
        parsed = json.loads(data)
    except ValueError as exc:  # 包括 UnicodeDecodeError
        raise ValueError("统计数据格式不对") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get(TOP_KEYS[kind]), list):
        raise ValueError("统计数据格式不对")
    return data


def ensure_stats_json(
    cache_dir: Path,
    kind: Kind,
    mmr: int,
    period: str,
    *,
    opener: Opener = _open,
    now: Callable[[], float] = time.time,
    max_age: float = MAX_AGE_SECONDS,
) -> Path:
    """返回本地缓存的统计文件；缓存过期时重新下载，下载失败则沿用旧缓存（没有旧缓存时抛出）。"""
    path = cache_dir / cache_name(kind, mmr, period)
    if path.exists() and now() - path.stat().st_mtime < max_age:
        return path
    try:
        data = _download(stats_url(kind, mmr, period), kind, opener)
    except (OSError, http.client.HTTPException, ValueError):
        if path.exists():
            log.warning("下载统计数据失败，继续使用旧缓存 %s", path, exc_info=True)
            return path
        raise
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path
