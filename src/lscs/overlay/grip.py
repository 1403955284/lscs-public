"""拖动标签（tkinter）：面板右上角的小窗口"≡ 拖动"，按住它把面板拖到别处，双击恢复默认位置
（面板拖动设计文档第 3 节）。

面板整块鼠标穿透（WS_EX_TRANSPARENT 是整个窗口的属性），自己收不到鼠标，所以拖动靠这个单独的
小窗口。它和画面高亮一样是同一个 Tk 的另一个 Toplevel：
- Windows：分层（和面板同一个不透明度）、不进任务栏、不抢焦点（WS_EX_NOACTIVATE），但**不**
  穿透；按下、拖动、松手、双击时炉石一直在前台。每次定位时检查样式还在不在（apply_style）；
- 非 Windows（replay --overlay 预览）和 Windows 的退路：普通置顶窗口（-alpha）；
- 隐藏时移到屏幕外、透明度设成 0，从不 withdraw / deiconify，也从不 focus_set / grab_set。

鼠标事件只绑在 Toplevel 上：标签里的 Label 的 bindtags 里也有这个 Toplevel，所以在字上按也一样；
两处都绑的话同一个事件会触发两次。按下左键后 Tk 自动捕获鼠标，拖出标签外照样收到移动和松手。
回调拿到的是屏幕坐标（x_root / y_root），和面板的位置同一个坐标系。

只在主线程使用。出错时抛出异常，由 OverlayWindow 决定怎么处理（关掉标签，面板照常）。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable

from lscs.overlay import win32
from lscs.overlay.geometry import Rect
from lscs.overlay.theme import BG, MUTED, OFFSCREEN

TEXT = "≡ 拖动"  # ≡（U+2261）中日韩字体都有
Point = Callable[[int, int], None]  # 屏幕坐标


class GripWindow:
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
        self._opacity = opacity
        self._native = native
        self._hwnd: int | None = None
        self._visible = True
        self._rect: Rect | None = None
        self._top = tk.Toplevel(root)
        try:
            self._setup(font, on_press, on_motion, on_release, on_reset)
        except BaseException:
            self.destroy()
            raise

    def _setup(
        self,
        font: tkfont.Font,
        on_press: Point,
        on_motion: Point,
        on_release: Point,
        on_reset: Callable[[], None],
    ) -> None:
        top = self._top
        top.title("lscs 拖动")
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=BG)
        top.geometry(OFFSCREEN)
        self._label = tk.Label(
            top,
            text=TEXT,
            fg=MUTED,
            bg=BG,
            font=font,
            cursor="fleur",
            anchor="center",
            padx=0,
            pady=0,
            borderwidth=0,
        )
        self._label.pack(fill="both", expand=True)
        top.bind("<ButtonPress-1>", lambda e: on_press(e.x_root, e.y_root))
        top.bind("<B1-Motion>", lambda e: on_motion(e.x_root, e.y_root))
        top.bind("<ButtonRelease-1>", lambda e: on_release(e.x_root, e.y_root))
        top.bind("<Double-Button-1>", lambda _e: on_reset())
        top.update()  # 让 Tk 创建顶层窗口，才能拿到句柄
        if self._native:
            self.apply_style()
        else:
            top.attributes("-alpha", self._opacity)

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    def apply_style(self) -> bool:
        """Windows：加上（或重新加上）分层、不进任务栏、不抢焦点、不穿透的样式；Tk 重建了顶层
        窗口时也能恢复。返回 True 表示句柄变了（重建过，不算第一次）：调用方要把它放回高亮上面。
        普通置顶窗口什么都不做。"""
        if not self._native:
            return False
        hwnd = win32.root_window(self._top.winfo_id())
        if hwnd is None:
            raise RuntimeError("拿不到拖动标签的窗口句柄（GetAncestor 返回空）")
        if hwnd == self._hwnd and win32.has_overlay_style(hwnd, False):
            return False
        if not win32.make_overlay(hwnd, self._opacity, False):  # 失败时抛出 Win32Error
            raise RuntimeError("不是 Windows")
        rebuilt = self._hwnd is not None and hwnd != self._hwnd
        self._hwnd = hwnd
        self._visible = True  # make_overlay 把透明度设回了 opacity
        return rebuilt

    def use_plain(self) -> None:
        """面板退回普通置顶窗口时，标签也一样（不再检查样式、不再隐藏）。"""
        self._native = False
        self._top.attributes("-alpha", self._opacity)
        self._visible = True

    def set_visible(self, visible: bool) -> None:
        """Windows：显示用面板的不透明度；隐藏时先移到屏幕外、再把透明度设成 0，不靠"透明度 0
        时鼠标穿过去"也不会挡住别的程序。再显示前调用方先 place() 放回原位。失败时抛出异常。"""
        if not self._native or self._hwnd is None or visible == self._visible:
            return
        if not visible:
            self._top.geometry(OFFSCREEN)
            self._rect = None  # 下次 place() 一定重新放
        if not win32.set_opacity(self._hwnd, self._opacity if visible else 0.0):
            raise RuntimeError("无法设置拖动标签的透明度")
        self._visible = visible

    def set_font(self, font: tkfont.Font) -> None:
        self._label.configure(font=font)

    def place(self, rect: Rect) -> None:
        if rect != self._rect:
            self._top.geometry(f"{rect.width}x{rect.height}+{rect.x}+{rect.y}")
            self._rect = rect

    def lift(self) -> None:
        self._top.lift()

    def destroy(self) -> None:
        try:
            self._top.destroy()
        except tk.TclError:
            pass

    # ---- 冒烟测试用 ----

    def text(self) -> str:
        return self._label.cget("text")

    @property
    def geometry(self) -> Rect | None:
        return self._rect

    @property
    def label(self) -> tk.Label:
        return self._label
