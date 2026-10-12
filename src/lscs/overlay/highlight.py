"""画面高亮窗口（tkinter）：盖在炉石客户区上的置顶窗口，只给"下一步"指的那张牌或那个按钮
画一个发光的框和步骤编号（选项建议设计文档第 8 节）。

- Windows：画布背景是色键 KEY，-transparentcolor 让没画东西的地方完全透明；make_passthrough
  再加上鼠标穿透、不进任务栏、不抢焦点的样式（只改扩展样式）。每次定位时检查样式还在不在；
- 预览（preview=True，非 Windows 上的 lscs highlight-test）：深色背景的普通置顶窗口；
- 隐藏时只清空画布（整窗透明，鼠标也穿过去），不 withdraw；不做闪烁和动画。

只在主线程使用。出错时抛出异常，由调用方（OverlayWindow、highlight-test）决定怎么处理。
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont

from lscs.config import HighlightConfig
from lscs.overlay import win32
from lscs.overlay.geometry import Rect
from lscs.overlay.layout import Frame, HighlightLayout, content_rect, step_frames
from lscs.overlay.model import NextStep

KEY = "#010203"  # 色键：画布上这个颜色的地方完全透明
PREVIEW_BG = "#1e1e24"
OUTER = "#8a6a1a"  # 外层描边：暗金
INNER = "#fff6cc"  # 内层描边：浅黄（中层用 [highlight] color）
BADGE_TEXT = "#1e1e24"
OUTER_WIDTH, MIDDLE_WIDTH, INNER_WIDTH = 0.012, 0.007, 0.003  # 内容区高度的比例
BADGE_RADIUS = 0.022
CORNER = 0.015  # 圆角半径
LABEL_FONT = 0.022  # highlight-test 标签的字号
OFFSCREEN = "1x1+-32000+-32000"
FAMILIES = {"win32": "Microsoft YaHei UI", "darwin": "PingFang SC"}


def _rounded(x0: float, y0: float, x1: float, y1: float, r: float) -> list[float]:
    """圆角矩形的多边形顶点（配合 smooth=True）。"""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    return [
        *(x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r),
        *(x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1),
        *(x0, y1, x0, y1 - r, x0, y0 + r, x0, y0),
    ]


class HighlightWindow:
    def __init__(self, root: tk.Misc, cfg: HighlightConfig, preview: bool = False) -> None:
        self._layout = HighlightLayout.from_config(cfg)
        self._color = cfg.color
        self._preview = preview
        self._hwnd: int | None = None
        self._client: Rect | None = None  # 窗口现在的位置（炉石客户区）
        self._drawn: tuple | None = None  # 画布上现在画的 (框, 位置)
        self._fonts: dict[int, tkfont.Font] = {}
        self._top = tk.Toplevel(root)
        try:
            self._setup()
        except BaseException:
            self.destroy()
            raise

    def _setup(self) -> None:
        top = self._top
        top.title("lscs 高亮")
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        background = PREVIEW_BG if self._preview else KEY
        if not self._preview:
            top.attributes("-transparentcolor", KEY)  # 只有 Windows 的 Tk 支持
        top.configure(bg=background)
        top.geometry(OFFSCREEN)
        self._canvas = tk.Canvas(top, bg=background, highlightthickness=0, borderwidth=0)
        self._canvas.pack(fill="both", expand=True)
        top.update()  # 让 Tk 创建顶层窗口，才能拿到句柄
        wanted = FAMILIES.get(sys.platform)
        if wanted and wanted in tkfont.families(top):
            self._family = wanted
        else:
            self._family = tkfont.nametofont("TkDefaultFont", root=top).actual("family")
        if not self._preview:
            self._apply_style()

    @property
    def layout(self) -> HighlightLayout:
        return self._layout

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    def _apply_style(self) -> bool:
        """加上（或重新加上）鼠标穿透的样式；Tk 重建了顶层窗口时也能恢复。
        返回 True 表示句柄变了（重建过，不算第一次）：调用方要把面板重新放到它上面。"""
        hwnd = win32.root_window(self._top.winfo_id())
        if hwnd is None:
            raise RuntimeError("拿不到画面高亮的窗口句柄（GetAncestor 返回空）")
        if hwnd == self._hwnd and win32.has_passthrough_style(hwnd):
            return False
        if not win32.make_passthrough(hwnd):  # 失败时抛出 Win32Error
            raise RuntimeError("不是 Windows")
        rebuilt = self._hwnd is not None and hwnd != self._hwnd
        self._hwnd = hwnd
        return rebuilt

    # ---- 显示 ----

    def show(self, step: NextStep | None, client: Rect | None) -> bool:
        """client 是炉石客户区（屏幕坐标）；None 表示面板隐藏了：清空，整窗透明。
        返回 True 表示 Tk 重建了顶层窗口（见 draw）。"""
        if client is None:
            return self.draw([], None)
        return self.draw(step_frames(self._layout, step, client), client)

    def draw(self, frames: list[Frame], client: Rect | None) -> bool:
        """把框画在 client 上（框的坐标和 client 同一个坐标系）；client 为 None 时只清空。
        返回 True 表示 Tk 重建了顶层窗口（新的句柄，样式已经重新加上）。"""
        if client is not None and client != self._client:
            self._top.geometry(f"{client.width}x{client.height}+{client.x}+{client.y}")
            self._client = client
        rebuilt = self._apply_style() if not self._preview else False
        key = (tuple(frames), client)
        if key == self._drawn:
            return rebuilt
        self._drawn = key
        self._canvas.delete("all")
        if client is not None:
            height = content_rect(client, self._layout.aspect).height
            for frame in frames:
                self._draw_frame(frame, client, height)
        return rebuilt

    def _font(self, px: int) -> tkfont.Font:
        if px not in self._fonts:
            self._fonts[px] = tkfont.Font(
                root=self._top, family=self._family, size=-px, weight="bold"
            )
        return self._fonts[px]

    def _draw_frame(self, frame: Frame, client: Rect, height: int) -> None:
        rect = frame.rect
        x0, y0 = rect.x - client.x, rect.y - client.y
        x1, y1 = x0 + rect.width, y0 + rect.height
        canvas = self._canvas
        # 三层描边模拟发光：外层宽、暗金；中层金黄；内层细、浅黄
        layers = (
            (OUTER, max(3, round(OUTER_WIDTH * height))),
            (self._color, max(2, round(MIDDLE_WIDTH * height))),
            (INNER, max(1, round(INNER_WIDTH * height))),
        )
        for color, width in layers:
            if frame.round:
                canvas.create_oval(x0, y0, x1, y1, outline=color, width=width)
            else:
                points = _rounded(x0, y0, x1, y1, CORNER * height)
                canvas.create_polygon(points, smooth=True, outline=color, fill="", width=width)
        if not frame.label:
            return
        if frame.badge:  # 步骤编号或"选"：框左上角一个实心圆徽章
            r = max(8, round(BADGE_RADIUS * height))
            canvas.create_oval(
                x0 - r, y0 - r, x0 + r, y0 + r, fill=self._color, outline=OUTER, width=2
            )
            canvas.create_text(x0, y0, text=frame.label, fill=BADGE_TEXT, font=self._font(r))
        else:  # highlight-test 的标签（"商店1""升本"）：写在框的正中间
            px = max(10, round(LABEL_FONT * height))
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            canvas.create_text(
                cx,
                cy,
                text=frame.label,
                fill=self._color,
                font=self._font(px),
                anchor="center",
                justify="center",
            )

    def destroy(self) -> None:
        self._fonts.clear()
        try:
            self._top.destroy()
        except tk.TclError:
            pass

    # ---- 冒烟测试用 ----

    def texts(self) -> list[str]:
        canvas = self._canvas
        return [canvas.itemcget(i, "text") for i in canvas.find_all() if canvas.type(i) == "text"]

    def shapes(self) -> int:
        return len(self._canvas.find_all())

    @property
    def geometry(self) -> Rect | None:
        return self._client
