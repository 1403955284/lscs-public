"""下载并缓存 HearthstoneJSON 的中文卡牌数据。"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import BinaryIO

log = logging.getLogger(__name__)

CARDS_URL = "https://api.hearthstonejson.com/v1/latest/zhCN/cards.json"
USER_AGENT = "lscs/0.1 (+https://github.com)"  # 默认的 Python-urllib UA 会被拒绝（403）
CACHE_NAME = "cards.zhCN.json"
MAX_AGE_SECONDS = 24 * 3600
TIMEOUT_SECONDS = 30


def default_cache_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "lscs" / "cache"
    return Path.home() / "Library" / "Caches" / "lscs"


def _open(request: urllib.request.Request) -> BinaryIO:
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def _download(url: str, opener: Callable[[urllib.request.Request], BinaryIO]) -> bytes:
    """下载并确认内容是 JSON 列表（防止把登录认证页 之类的网页缓存成卡牌数据）。"""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener(request) as response:
        data = response.read()
    if not isinstance(json.loads(data), list):
        raise ValueError("卡牌数据不是 JSON 列表")
    return data


def ensure_cards_json(
    cache_dir: Path,
    *,
    url: str = CARDS_URL,
    max_age: float = MAX_AGE_SECONDS,
    opener: Callable[[urllib.request.Request], BinaryIO] = _open,
    now: Callable[[], float] = time.time,
) -> Path:
    """返回本地缓存的 cards.json；缓存过期时重新下载，下载失败则沿用旧缓存。"""
    path = cache_dir / CACHE_NAME
    if path.exists() and now() - path.stat().st_mtime < max_age:
        return path
    try:
        data = _download(url, opener)
    except (OSError, http.client.HTTPException, ValueError):
        if path.exists():
            log.warning("下载卡牌数据失败，继续使用旧缓存 %s", path, exc_info=True)
            return path
        raise
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path
