"""对手小标签（tkinter）：面板上方、"≡ 拖动"左边写着"对手"的小窗口，点一下打开对手阵容卡图
窗口，再点一下关掉（对手阵容卡图窗口设计文档第 3 节）。

窗口本身（不穿透、不抢焦点、隐藏时移到屏幕外）见 overlay.tag.TagWindow；这里只有两个鼠标绑定和
开 / 关两种颜色。松手时指针还在标签里才算点了一下（由 OverlayWindow 判断）；不绑双击（双击 =
开再关）。

只在主线程使用。出错时抛出异常，由 OverlayWindow 决定怎么处理（关掉标签和卡图窗口，面板照常）。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable

from lscs.overlay.tag import TagWindow
from lscs.overlay.theme import BG, MUTED, NOW_BG, OPP_TAG

TEXT = "对手"
CLOSED = (MUTED, BG)  # 关着：和"≡ 拖动"一样
OPEN = (OPP_TAG, NOW_BG)  # 开着：淡紫字、比面板底色亮一级的底
Point = Callable[[int, int], None]  # 屏幕坐标


class TabWindow(TagWindow):
    def __init__(
        self,
        root: tk.Misc,
        font: tkfont.Font,
        opacity: float,
        native: bool,
        on_press: Point,
        on_release: Point,
    ) -> None:
        """native：Windows 上贴靠炉石（加不抢焦点的样式）；否则是普通置顶窗口。"""
        super().__init__(
            root, TEXT, font, opacity, native, cursor="hand2", title="lscs 对手", name="对手标签"
        )
        self._open = False
        top = self._top
        try:
            top.bind("<ButtonPress-1>", lambda e: on_press(e.x_root, e.y_root))
            top.bind("<ButtonRelease-1>", lambda e: on_release(e.x_root, e.y_root))
        except BaseException:
            self.destroy()
            raise

    def set_open(self, is_open: bool) -> None:
        """开 / 关两种颜色；文字和宽度不变。没变时什么都不做（每次定位都会调用）。"""
        if is_open != self._open:
            self.set_colors(*(OPEN if is_open else CLOSED))
            self._open = is_open

    # ---- 冒烟测试用 ----

    def colors(self) -> tuple[str, str]:
        return self._label.cget("fg"), self._label.cget("bg")
