"""对手阵容卡图窗口的肖像照片（对手阵容卡图窗口设计文档第 6.3 节）：把 ArtCache 解码好的原画裁成
4:5、缩放、套上椭圆遮罩（边缘平滑），做成 Tk 能画的 ImageTk.PhotoImage。

**只在 Tk 的主线程使用**（后台线程只产出 Pillow 的图）。ImageTk.PhotoImage 只有被 Python 引用着
才存在，画布上的 image 项不算引用：这里的字典就是那个引用（最多 MAX_PHOTOS 张，最近用过的
留着）；卡图窗口持有它，销毁窗口前先 clear()。

没有 Pillow（或 Pillow 没有 Tk 支持）、没有缓存、原图还没到时 photo() 返回 None：卡图窗口画深色
椭圆 + 名字。
"""

from __future__ import annotations

import logging
import tkinter as tk
from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Protocol

from lscs.overlay.boards import PORTRAIT_ASPECT

if TYPE_CHECKING:
    from PIL.Image import Image

log = logging.getLogger(__name__)

# 1440p 下一张 63×79 RGBA 约 20 KB；不小于 SLOTS × MAX_OPPONENTS（49）：一屏的卡图不会互相挤掉
MAX_PHOTOS = 160
SUPERSAMPLE = 4  # 椭圆遮罩先画 4 倍大再缩回：边缘平滑


class ArtSource(Protocol):
    """ArtCache 的这几样（测试里换成内存里的假缓存）。"""

    @property
    def available(self) -> bool: ...

    @property
    def generation(self) -> int: ...

    def image(self, card_id: str) -> Image | None: ...


def _image_tk() -> Any | None:
    try:
        from PIL import ImageTk
    except ImportError:
        return None
    return ImageTk


def oval_portrait(image: Image, d: int) -> Image:
    """原图居中裁成 4:5 → 缩放成 d × round(1.25 d) → 椭圆以外透明（RGBA）。"""
    from PIL import Image as PILImage
    from PIL import ImageDraw

    width, height = image.size
    if width * PORTRAIT_ASPECT > height:  # 太宽：裁两边
        crop_w, crop_h = round(height / PORTRAIT_ASPECT), height
    else:  # 太高：裁上下
        crop_w, crop_h = width, round(width * PORTRAIT_ASPECT)
    left, top = (width - crop_w) // 2, (height - crop_h) // 2
    size = (d, round(PORTRAIT_ASPECT * d))
    resample = PILImage.Resampling.LANCZOS
    portrait = image.crop((left, top, left + crop_w, top + crop_h)).resize(size, resample)
    big = (size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE)
    mask = PILImage.new("L", big, 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big[0] - 1, big[1] - 1), fill=255)
    portrait = portrait.convert("RGBA")
    portrait.putalpha(mask.resize(size, resample))
    return portrait


class Portraits:
    def __init__(self, cache: ArtSource | None, root: tk.Misc) -> None:
        self._cache = cache
        self._root = root
        self._image_tk = _image_tk() if cache is not None and cache.available else None
        self._photos: OrderedDict[tuple[str, int], Any] = OrderedDict()

    @property
    def available(self) -> bool:
        """能画卡图（有缓存、有 Pillow 和它的 Tk 支持）。"""
        return self._image_tk is not None

    @property
    def generation(self) -> int:
        """缓存每到一张图加一：变了就该重画。"""
        return self._cache.generation if self._cache is not None else 0

    def photo(self, card_id: str, d: int) -> Any | None:
        """card_id 的椭圆肖像（宽 d）；原图还没到、没有 Pillow 时 None。同一个 (card_id, d)
        返回同一个对象。"""
        if self._image_tk is None or self._cache is None or d <= 0:
            return None
        key = (card_id, d)
        photo = self._photos.get(key)
        if photo is not None:
            self._photos.move_to_end(key)
            return photo
        image = self._cache.image(card_id)
        if image is None:
            return None
        photo = self._image_tk.PhotoImage(oval_portrait(image, d), master=self._root)
        self._photos[key] = photo
        while len(self._photos) > MAX_PHOTOS:
            self._photos.popitem(last=False)
        return photo

    def clear(self) -> None:
        self._photos.clear()

    def __len__(self) -> int:
        return len(self._photos)
