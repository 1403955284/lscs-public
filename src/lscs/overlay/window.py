"""悬浮面板窗口（tkinter）：画出 OverlayView，贴到炉石窗口上，按前台窗口显示或隐藏。

只在主线程使用。定时器回调里的任何异常都只记日志，定时器照常继续。
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable

from lscs.config import OverlayConfig
from lscs.overlay import geometry, win32
from lscs.overlay.geometry import Layout, Rect
from lscs.overlay.model import OverlayModel, OverlayView, condense

log = logging.getLogger(__name__)

RENDER_MS = 150  # 读取数据模型的间隔
CONDENSE_LEVELS = 3  # 0 全部；1 去掉"方向"；2 再去掉"要找"和附注、"目标"只留一行
PLACE_MS = 500  # 重新定位、判断显示/隐藏的间隔
BG = "#1e1e24"
TITLE = "#d4d4dc"
RULES_TAG = "#e6b450"  # 金黄
AI_TAG = "#4fc9e0"  # 青色
TEXT = "#f0f0f4"
MUTED = "#8c8c96"
WARN = "#ff9f43"  # 橙色
FAMILIES = {"win32": "Microsoft YaHei UI", "darwin": "PingFang SC"}
SIDES = {"right": "右侧", "left": "左侧", "top": "顶部"}
OFFSCREEN = "1x1+-32000+-32000"  # 第一次显示时放在屏幕外，免得在左上角闪一下
DOCK_FAILURE_LIMIT = 5  # 贴靠炉石连续失败几次后改用普通置顶窗口
FALLBACK_HINT = "面板已移到屏幕右上角；不需要面板可以用 --no-overlay 关闭"

Output = Callable[[str], None]


def _print(line: str) -> None:
    print(line, flush=True)


def _reason(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def _font_family(root: tk.Tk) -> str:
    wanted = FAMILIES.get(sys.platform)
    if wanted and wanted in tkfont.families(root):
        return wanted
    return tkfont.nametofont("TkDefaultFont", root=root).actual("family")


class OverlayWindow:
    def __init__(self, cfg: OverlayConfig, out: Output = _print) -> None:
        self._cfg = cfg
        self._out = out
        previous = None
        if win32.IS_WINDOWS:
            try:
                win32.set_dpi_awareness()  # 必须在创建窗口之前
            except Exception:
                log.warning("设置 DPI 感知失败，面板位置可能有偏差", exc_info=True)
            try:
                previous = win32.foreground_window()  # 一般是运行 watch 的 PowerShell
            except Exception:
                log.debug("取前台窗口失败", exc_info=True)
        self._root = tk.Tk()
        try:
            self._setup(previous)
        except BaseException:
            self._root.__dict__.pop("report_callback_exception", None)  # 拆掉 root ↔ self 的环
            try:
                self._root.destroy()
            except tk.TclError:
                pass
            raise

    def _setup(self, previous: int | None) -> None:
        root, cfg = self._root, self._cfg
        root.title("lscs")
        root.report_callback_exception = self._callback_error
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.configure(bg=BG)
        root.geometry(OFFSCREEN)
        self._frame = tk.Frame(root, bg=BG)
        self._frame.pack(fill="both", expand=True)
        self._family = _font_family(root)
        self._fonts: dict[tuple[int, bool], tkfont.Font] = {}
        self._model: OverlayModel | None = None
        self._keep_running: Callable[[], bool] = lambda: True
        self._view: OverlayView | None = None
        self._layout: Layout | None = None
        self._size = (1, 1)
        self._geometry: tuple[int, int, int, int] | None = None
        self._visible = True
        self._closing = False
        self._errors: set[str] = set()
        self._hwnd: int | None = None
        self._native = False
        self._dock_failures = 0
        # 让 Tk 创建顶层窗口，才能拿到句柄。Tk 只在第一次显示时才创建它，所以没法在显示之前
        # 加上"不抢焦点"的样式；显示后如果抢走了 PowerShell 的焦点，下面再还回去。
        root.update()
        side = SIDES[cfg.position]
        if not win32.IS_WINDOWS:
            root.attributes("-alpha", cfg.opacity)
            self.status = "悬浮面板已打开（不是 Windows，固定在屏幕右上角，不穿透鼠标）。"
            return
        self._locator = win32.HearthstoneLocator()
        try:
            self._apply_style()
            self._native = True
            self.status = (
                f"悬浮面板已打开：炉石在前台时显示在游戏窗口{side}，切到别的窗口时自动隐藏。"
            )
        except Exception as exc:
            log.warning("设置面板窗口样式失败", exc_info=True)
            self._use_plain_window()
            self.status = (
                f"无法让悬浮面板贴靠炉石窗口、穿透鼠标（{_reason(exc)}），{FALLBACK_HINT}。"
                "可以运行 uv run lscs overlay-check 查看详情。"
            )
        self._give_focus_back(previous)

    def _give_focus_back(self, previous: int | None) -> None:
        """面板第一次显示时可能抢走 PowerShell 的焦点（之后按 Ctrl+C 没反应），还给它。"""
        try:
            panel = win32.root_window(self._root.winfo_id())
            if previous and panel and previous != panel and win32.foreground_window() == panel:
                win32.set_foreground(previous)
        except Exception:
            log.debug("把焦点还给原来的窗口时出错", exc_info=True)

    # ---- 主循环 ----

    def run(self, model: OverlayModel, keep_running: Callable[[], bool]) -> None:
        self._model = model
        self._keep_running = keep_running
        self._render_tick()
        self._place_tick()
        try:
            if not self._closing:
                self._root.mainloop()
        finally:
            self._release()

    def _release(self) -> None:
        """销毁窗口并拆掉引用环，让 Tk 对象在主线程里就被释放。"""
        try:
            self._root.destroy()
        except tk.TclError:
            pass
        self._fonts.clear()
        self._root.__dict__.pop("report_callback_exception", None)  # root ↔ self 的引用环
        self._model = None
        self._keep_running = lambda: False

    def _close(self) -> None:
        self._closing = True
        self._root.quit()

    def _render_tick(self) -> None:
        if self._closing:
            return
        try:
            if not self._keep_running():
                self._close()
                return
            view = self._model.snapshot() if self._model is not None else None
            if view is not None and (self._view is None or view.version != self._view.version):
                self._view = view
                self._draw()
                self._place()  # 内容变了，高度也可能变
        except Exception:
            self._report("刷新悬浮面板时出错")
        self._root.after(RENDER_MS, self._render_tick)

    def _place_tick(self) -> None:
        if self._closing:
            return
        try:
            self._place()
        except Exception:
            self._report("移动悬浮面板时出错")
        self._root.after(PLACE_MS, self._place_tick)

    def _report(self, message: str) -> None:
        """同一种错误只打印一次完整堆栈，免得每 150 毫秒刷一次屏。"""
        key = f"{message}:{sys.exc_info()[0]}:{sys.exc_info()[1]}"
        if key in self._errors:
            log.debug(message, exc_info=True)
            return
        self._errors.add(key)
        log.warning(message, exc_info=True)

    def _callback_error(self, exc_type, exc, tb) -> None:
        log.warning("悬浮面板回调出错", exc_info=(exc_type, exc, tb))

    # ---- 位置与显示 ----

    def _screen(self) -> Rect:
        return Rect(0, 0, self._root.winfo_screenwidth(), self._root.winfo_screenheight())

    def _apply_style(self) -> None:
        """设置（或重新设置）鼠标穿透等窗口样式；Tk 重建了顶层窗口时也能恢复。"""
        cfg = self._cfg
        hwnd = win32.root_window(self._root.winfo_id())
        if hwnd is None:
            raise RuntimeError("拿不到面板的窗口句柄（GetAncestor 返回空）")
        if hwnd == self._hwnd and win32.has_overlay_style(hwnd, cfg.click_through):
            return
        if not win32.make_overlay(hwnd, cfg.opacity, cfg.click_through):  # 失败时抛出 Win32Error
            raise RuntimeError("不是 Windows")
        self._hwnd = hwnd
        self._visible = True  # make_overlay 把透明度设回了 opacity

    def _use_plain_window(self) -> None:
        self._native = False
        self._root.attributes("-alpha", self._cfg.opacity)
        self._visible = True

    def _target(self) -> tuple[Rect, bool] | None:
        """(参照区域, 是否贴靠炉石)；None 表示隐藏。"""
        if self._native:
            try:
                target = self._hearthstone_target()
            except Exception as exc:
                # 偶尔失败（比如炉石正好在关闭）先隐藏，连续失败才改用普通置顶窗口
                self._dock_failures += 1
                self._report("贴靠炉石窗口时出错")
                if self._dock_failures < DOCK_FAILURE_LIMIT:
                    return None
                self._use_plain_window()
                self._out(
                    f"贴靠炉石窗口连续 {DOCK_FAILURE_LIMIT} 次失败（{_reason(exc)}），"
                    f"{FALLBACK_HINT}。"
                )
            else:
                self._dock_failures = 0
                return target
        return self._screen(), False

    def _hearthstone_target(self) -> tuple[Rect, bool] | None:
        self._apply_style()
        hearthstone = self._locator.get()
        minimized = hearthstone is not None and win32.is_minimized(hearthstone)
        foreground = win32.foreground_window()
        if not win32.should_show(hearthstone, foreground, self._hwnd, minimized):
            return None
        client = win32.client_rect(hearthstone)
        return (client, True) if client is not None else None

    def _place(self) -> None:
        target = self._target()
        if target is None:
            self._set_visible(False)
            return
        area, docked = target
        layout = geometry.panel_layout(area, self._cfg.font_size)
        if layout != self._layout:
            self._layout = layout
            self._draw()
        width, height = self._size
        if docked:
            screen = (win32.virtual_screen() if self._native else None) or self._screen()
            x, y = geometry.dock(area, screen, width, height, self._cfg.position)
        else:
            x, y = geometry.corner(area, width, height)
        if (width, height, x, y) != self._geometry:
            self._geometry = (width, height, x, y)
            self._root.geometry(f"{width}x{height}+{x}+{y}")
        self._set_visible(True)

    def _set_visible(self, visible: bool) -> None:
        """只有贴靠炉石时才会隐藏。隐藏用透明度 0 而不是 withdraw()：窗口一直在，
        不会因为重新显示而抢走炉石的焦点，Tk 也不会重建顶层窗口、丢掉穿透样式。"""
        if visible == self._visible or not self._native or self._hwnd is None:
            return
        if not win32.set_opacity(self._hwnd, self._cfg.opacity if visible else 0.0):
            raise RuntimeError("无法设置面板的透明度")
        self._visible = visible

    # ---- 绘制 ----

    def _font(self, px: int, bold: bool = False) -> tkfont.Font:
        key = (px, bold)
        if key not in self._fonts:
            weight = "bold" if bold else "normal"
            self._fonts[key] = tkfont.Font(
                root=self._root, family=self._family, size=-px, weight=weight
            )
        return self._fonts[key]

    def _label(self, text: str, color: str, font: tkfont.Font) -> tk.Label:
        return tk.Label(
            self._frame,
            text=text,
            fg=color,
            bg=BG,
            font=font,
            justify="left",
            anchor="w",
            padx=0,
            pady=0,
            borderwidth=0,
        )

    def _draw(self) -> None:
        """画出当前内容；超过最大高度时逐级精简（先去掉"方向"，再去掉"要找"和附注、
        "目标"只留一行），最后仍然超高就截掉底部。"""
        view, layout = self._view, self._layout
        if view is None or layout is None:
            return
        height = 0
        for level in range(CONDENSE_LEVELS):
            height = self._draw_level(condense(view, level), layout, level)
            if height <= layout.max_height or view.compact:
                break
        width = layout.width
        if view.compact:  # 只有一行时按文字宽度收窄
            bold = self._font(layout.font_px, bold=True)
            width = min(layout.width, bold.measure(view.title) + 2 * layout.pad + 2)
        self._size = (max(width, 1), max(min(height, layout.max_height), 1))

    def _draw_level(self, view: OverlayView, layout: Layout, level: int) -> int:
        """重建所有标签，返回需要的高度。"""
        for child in self._frame.winfo_children():
            child.destroy()
        text_font, bold = self._font(layout.font_px), self._font(layout.font_px, bold=True)
        pad = layout.pad
        inner = layout.width - 2 * pad
        gap = max(4, layout.font_px // 2)
        tag_width = max(bold.measure("规则"), bold.measure("AI")) + gap
        self._frame.configure(padx=pad, pady=pad)
        self._frame.grid_columnconfigure(0, minsize=tag_width if not view.compact else 0)

        def wrapped(text: str, width: int, font: tkfont.Font, max_lines: int = 0) -> str:
            lines = geometry.wrap(text, max(width, 1), font.measure)
            if max_lines:
                lines = geometry.clip(lines, max_lines, max(width, 1), font.measure)
            return "\n".join(lines)

        row = 0
        title = self._label(wrapped(view.title, inner, bold), TITLE, bold)
        title.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        sections = (("规则", view.rules, RULES_TAG), (view.ai_title, view.ai, AI_TAG))
        for tag, lines, color in sections if not view.compact else ():
            if not lines:
                continue
            first_pad = gap // 2
            label = self._label(tag, color, bold)
            if bold.measure(tag) + gap > tag_width:
                # 放不进标签列的标题（"AI（深度）"）单独占一行，内容仍然从第二列开始
                label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(first_pad, 0))
                row += 1
                first_pad = 0
            else:
                label.grid(row=row, column=0, sticky="nw", pady=(first_pad, 0))
            for i, line in enumerate(lines):
                max_lines = 1 if level >= 2 and line.role == "reason" else 0
                text = wrapped(line.text, inner - tag_width, text_font, max_lines)
                label = self._label(text, MUTED if line.tone == "muted" else TEXT, text_font)
                label.grid(row=row, column=1, sticky="w", pady=(first_pad if i == 0 else 0, 0))
                row += 1
        if view.warning and not view.compact:
            warning = self._label(wrapped(view.warning, inner, text_font), WARN, text_font)
            warning.grid(row=row, column=0, columnspan=2, sticky="w", pady=(gap // 2, 0))
        self._root.update_idletasks()
        return self._frame.winfo_reqheight()

    # ---- 冒烟测试用 ----

    def texts(self) -> list[str]:
        return [w.cget("text") for w in self._frame.winfo_children() if isinstance(w, tk.Label)]

    @property
    def size(self) -> tuple[int, int]:
        return self._size
