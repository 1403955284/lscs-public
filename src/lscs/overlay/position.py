"""面板拖到的位置：存在项目文件夹的 overlay-position.json（不进仓库，不改 config.toml）。

内容是锚点（geometry.Anchor）：面板哪条边贴在炉石客户区的哪个比例上，所以换分辨率、拉窗口都
按比例跟着走。文件不存在、坏了、版本不对时 load 返回 None，面板用 [overlay] position 的预设；
下次拖动会覆盖掉坏文件。双击面板上的"≡ 拖动"或删掉文件就回到预设。
"""

from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path

from lscs.config import PROJECT_ROOT
from lscs.overlay.geometry import Anchor

log = logging.getLogger(__name__)

DEFAULT_PATH = PROJECT_ROOT / "overlay-position.json"  # 测试里换成临时目录（见 conftest）
VERSION = 1
HORIZONTAL = ("left", "center", "right")
VERTICAL = ("top", "bottom")


def _is_ratio(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return False
    return math.isfinite(value) and 0 <= value <= 1


def _parse(data: object) -> Anchor | None:
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if isinstance(version, bool) or version != VERSION:
        return None
    x, y, h, v = (data.get(key) for key in ("x", "y", "h", "v"))
    if not (_is_ratio(x) and _is_ratio(y)) or h not in HORIZONTAL or v not in VERTICAL:
        return None
    return Anchor(float(x), float(y), h, v)


def load(path: Path) -> Anchor | None:
    """读取记住的位置；没有文件或文件不能用时返回 None（用预设），从不抛出。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, ValueError):  # 读不了（权限、是个文件夹）或不是 UTF-8
        log.debug("读取面板位置文件 %s 失败", path, exc_info=True)
        return None
    try:
        anchor = _parse(json.loads(text))
    except ValueError:
        anchor = None
    if anchor is None:
        log.debug("面板位置文件 %s 内容不对，用预设位置", path)
    return anchor


def save(path: Path, anchor: Anchor) -> None:
    """先写 .tmp 再换过去，写到一半断电也不会留下半个文件。失败时抛出 OSError。"""
    data = {"version": VERSION, "x": anchor.x, "y": anchor.y, "h": anchor.h, "v": anchor.v}
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def clear(path: Path) -> None:
    """删掉记住的位置（恢复预设）；本来就没有也不报错。"""
    path.unlink(missing_ok=True)
