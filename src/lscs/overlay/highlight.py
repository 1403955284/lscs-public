"""画面高亮窗口（tkinter）：盖在炉石客户区上的置顶窗口，只给"下一步"指的那张牌或那个按钮
画四个角的括号和步骤编号（选项建议设计文档第 8 节；样式见文末"高亮样式"一节）。

- Windows：画布背景是色键 KEY，-transparentcolor 让没画东西的地方完全透明；make_passthrough
  再加上鼠标穿透、不进任务栏、不抢焦点的样式（只改扩展样式）。每次定位时检查样式还在不在；
- 预览（preview=True，非 Windows 上的 lscs highlight-test）：深色背景的普通置顶窗口；
- 隐藏时只清空画布（整窗透明，鼠标也穿过去），不 withdraw；
- 换到新目标时"呼吸"约 1.2 秒（style.Breathing，用 after 定时器、只改线条颜色、不重建图形）；
  换目标、隐藏、销毁时取消定时器，回调里的异常只记日志；
- 战斗中在棋盘顶部正中画一条战斗结果的横幅（提前显示战斗结果设计文档第 5.3 节）：深色圆角框，
  边框和字用结果的颜色，画在框之后（最上层），不呼吸。

几何和呼吸的状态机都在 overlay.style（纯函数），这里只管画。只在主线程使用。出错时抛出异常，
由调用方（OverlayWindow、highlight-test）决定怎么处理。
"""

from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font as tkfont
from functools import partial

from lscs.config import HighlightConfig
from lscs.overlay import win32
from lscs.overlay.banner import Banner
from lscs.overlay.geometry import Rect, clip, wrap
from lscs.overlay.layout import (
    Frame,
    HighlightLayout,
    banner_box,
    banner_font_px,
    banner_text_width,
    content_rect,
    step_frames,
)
from lscs.overlay.model import NextStep
from lscs.overlay.style import (
    BADGE_FILL,
    BADGE_RING,
    BANNER_COLORS,
    BANNER_RADIUS,
    BANNER_RING,
    CORNER,
    DARK,
    PULSE_MS,
    Breathing,
    Strokes,
    badge_circle,
    badge_font_px,
    corner_brackets,
    dimmed,
    rounded_points,
    stroke_widths,
)

KEY = "#010203"  # 色键：画布上这个颜色的地方完全透明
PREVIEW_BG = "#1e1e24"
LABEL_FONT = 0.022  # highlight-test 标签的字号
OFFSCREEN = "1x1+-32000+-32000"
FAMILIES = {"win32": "Microsoft YaHei UI", "darwin": "PingFang SC"}
# 呼吸时要换颜色的图形：线（fill）和圆、多边形的边（outline）
PULSE_FILL, PULSE_OUTLINE = "pulse_fill", "pulse_outline"


def _recolor(canvas: tk.Canvas, color: str, dim_color: str, dim: bool) -> None:
    """呼吸：只改颜色线和徽章外圈的颜色（不删掉重画，色键窗口不会闪）。"""
    now = dim_color if dim else color
    canvas.itemconfigure(PULSE_FILL, fill=now)
    canvas.itemconfigure(PULSE_OUTLINE, outline=now)


class HighlightWindow:
    def __init__(self, root: tk.Misc, cfg: HighlightConfig, preview: bool = False) -> None:
        self._layout = HighlightLayout.from_config(cfg)
        self._color = cfg.color
        self._dim_color = dimmed(cfg.color)
        self._style = cfg.style
        self._preview = preview
        self._hwnd: int | None = None
        self._client: Rect | None = None  # 窗口现在的位置（炉石客户区）
        self._drawn: tuple | None = None  # 画布上现在画的 (框, 位置, 横幅)
        self._fonts: dict[int, tkfont.Font] = {}
        self._top = tk.Toplevel(root)
        self._breathing: Breathing | None = None
        try:
            self._setup()
            # 不引用 self：HighlightWindow 和定时器之间没有引用环
            self._breathing = Breathing(
                self._top.after,
                self._top.after_cancel,
                partial(_recolor, self._canvas, self._color, self._dim_color),
                enabled=cfg.pulse,
                interval_ms=PULSE_MS,
            )
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

    def show(
        self, step: NextStep | None, client: Rect | None, banner: Banner | None = None
    ) -> bool:
        """client 是炉石客户区（屏幕坐标）；None 表示面板隐藏了：清空，整窗透明。
        banner：棋盘顶部的战斗结果（None 不画）。返回 True 表示 Tk 重建了顶层窗口（见 draw）。"""
        if client is None:
            return self.draw([], None)
        return self.draw(step_frames(self._layout, step, client), client, banner)

    def draw(self, frames: list[Frame], client: Rect | None, banner: Banner | None = None) -> bool:
        """把框（和横幅）画在 client 上（框的坐标和 client 同一个坐标系）；client 为 None 时
        只清空。返回 True 表示 Tk 重建了顶层窗口（新的句柄，样式已经重新加上）。"""
        if client is not None and client != self._client:
            self._top.geometry(f"{client.width}x{client.height}+{client.x}+{client.y}")
            self._client = client
        rebuilt = self._apply_style() if not self._preview else False
        shown = (banner.text, banner.tone) if banner is not None and client is not None else None
        key = (tuple(frames), client, shown)  # 文字或位置变了才重画；到期时间不算
        if key == self._drawn:
            return rebuilt
        self._drawn = key
        if self._breathing is not None:  # 先定下这次用原色还是暗色；校准框和横幅不呼吸
            steps = tuple(f for f in frames if not f.calibration) if client is not None else ()
            self._breathing.show(steps)
        self._canvas.delete("all")
        if client is not None:
            height = content_rect(client, self._layout.aspect).height
            for frame in frames:
                self._draw_frame(frame, client, height)
            if banner is not None:
                self._draw_banner(banner, client)
        return rebuilt

    def _font(self, px: int) -> tkfont.Font:
        if px not in self._fonts:
            self._fonts[px] = tkfont.Font(
                root=self._top, family=self._family, size=-px, weight="bold"
            )
        return self._fonts[px]

    def _draw_frame(self, frame: Frame, client: Rect, height: int) -> None:
        rect = Rect(
            frame.rect.x - client.x, frame.rect.y - client.y, frame.rect.width, frame.rect.height
        )
        widths = stroke_widths(height)
        dim = self._breathing is not None and self._breathing.dim
        color = self._dim_color if dim else self._color
        if frame.calibration or self._style == "frame":
            self._draw_outline(rect, frame.round, widths, color, height)
        else:
            self._draw_corners(rect, widths, color, height)
        if not frame.label:
            return
        canvas = self._canvas
        if frame.badge:  # 步骤编号或"选"：框右上角上方一个深色小圆，外圈和字用高亮颜色
            circle = badge_circle(rect, height)
            cx, cy, r = circle.cx, circle.cy, circle.r
            canvas.create_oval(
                cx - r,
                cy - r,
                cx + r,
                cy + r,
                fill=BADGE_FILL,
                outline=color,
                width=BADGE_RING,
                tags=PULSE_OUTLINE,
            )
            font = self._font(badge_font_px(circle))
            canvas.create_text(cx, cy, text=frame.label, fill=self._color, font=font)
        else:  # highlight-test 的标签（"商店1""升本"）：写在框的正中间
            px = max(10, round(LABEL_FONT * height))
            canvas.create_text(
                rect.x + rect.width / 2,
                rect.y + rect.height / 2,
                text=frame.label,
                fill=self._color,
                font=self._font(px),
                anchor="center",
                justify="center",
            )

    def _draw_banner(self, banner: Banner, client: Rect) -> None:
        """棋盘顶部正中的战斗结果：深色圆角框，边框和粗体字用结果的颜色。文字比半个内容区还宽
        时截掉末尾加"…"（现有的写法都放得下）。"""
        aspect = self._layout.aspect
        px = banner_font_px(client, aspect)
        font = self._font(px)
        limit = max(banner_text_width(client, aspect, px), 1)
        text = clip(wrap(banner.text, limit, font.measure), 1, limit, font.measure)[0]
        box = banner_box(client, aspect, font.measure(text), px)
        x0, y0 = box.x - client.x, box.y - client.y
        x1, y1 = x0 + box.width, y0 + box.height
        color = BANNER_COLORS.get(banner.tone, self._color)
        self._canvas.create_polygon(
            rounded_points(x0, y0, x1, y1, BANNER_RADIUS * box.height),
            smooth=True,
            fill=BADGE_FILL,
            outline=color,
            width=BANNER_RING,
        )
        self._canvas.create_text(
            (x0 + x1) / 2, (y0 + y1) / 2, text=text, fill=color, font=font, anchor="center"
        )

    def _draw_corners(self, rect: Rect, widths: Strokes, color: str, height: int) -> None:
        """四个角的括号：先画四条深色衬底，再在上面画颜色线（方头、尖角，看起来是实心的 L）。"""
        brackets = corner_brackets(rect, height)
        for width, fill, tags in ((widths.under, DARK, ()), (widths.line, color, PULSE_FILL)):
            for bracket in brackets:
                self._canvas.create_line(
                    *(c for point in bracket for c in point),
                    fill=fill,
                    width=width,
                    capstyle="projecting",
                    joinstyle="miter",
                    tags=tags,
                )

    def _draw_outline(
        self, rect: Rect, round_: bool, widths: Strokes, color: str, height: int
    ) -> None:
        """整圈细线框（style = "frame"、highlight-test 的校准框）：深色衬底上一条颜色线；
        按钮画圆，槽位画圆角矩形。"""
        x0, y0, x1, y1 = rect.x, rect.y, rect.right, rect.bottom
        for width, outline, tags in ((widths.under, DARK, ()), (widths.line, color, PULSE_OUTLINE)):
            if round_:
                self._canvas.create_oval(x0, y0, x1, y1, outline=outline, width=width, tags=tags)
            else:
                points = rounded_points(x0, y0, x1, y1, CORNER * height)
                self._canvas.create_polygon(
                    points, smooth=True, outline=outline, fill="", width=width, tags=tags
                )

    def destroy(self) -> None:
        if self._breathing is not None:
            self._breathing.stop()  # 先取消定时器：窗口没了它再触发就会出错
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
