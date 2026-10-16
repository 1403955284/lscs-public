"""拖动标签（tkinter）：面板右上角的小窗口"≡ 拖动"，按住它把面板拖到别处，双击恢复默认位置
（面板拖动设计文档第 3 节）。

窗口本身（不穿透、不抢焦点、隐藏时移到屏幕外）见 overlay.tag.TagWindow；这里只有鼠标绑定。
按下左键后 Tk 自动捕获鼠标，拖出标签外照样收到移动和松手。

只在主线程使用。出错时抛出异常，由 OverlayWindow 决定怎么处理（关掉标签，面板照常）。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable

from lscs.overlay.tag import TagWindow

TEXT = "≡ 拖动"  # ≡（U+2261）中日韩字体都有
Point = Callable[[int, int], None]  # 屏幕坐标


class GripWindow(TagWindow):
    def __init__(
        self,
        root: tk.Misc,
        font: tkfont.Font,
        opacity: float,
        native: bool,
        on_press: Point,
        on_motion: Point,
        on_release: Point,
        on_reset: Callable[[], None],
    ) -> None:
        """native：Windows 上贴靠炉石（加不抢焦点的样式）；否则是普通置顶窗口。"""
        super().__init__(
            root, TEXT, font, opacity, native, cursor="fleur", title="lscs 拖动", name="拖动标签"
        )
        top = self._top
        try:
            top.bind("<ButtonPress-1>", lambda e: on_press(e.x_root, e.y_root))
            top.bind("<B1-Motion>", lambda e: on_motion(e.x_root, e.y_root))
            top.bind("<ButtonRelease-1>", lambda e: on_release(e.x_root, e.y_root))
            top.bind("<Double-Button-1>", lambda _e: on_reset())
        except BaseException:
            self.destroy()
            raise
