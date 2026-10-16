"""悬浮面板窗口（tkinter）：画出 OverlayView，贴到炉石窗口上，按前台窗口显示或隐藏。
Windows 上贴靠炉石时，再开一个画面高亮窗口（overlay.highlight）盖在炉石客户区上，
给"下一步"画框、战斗中在棋盘顶部画战斗结果的横幅（OverlayView.banner，面板自己不画）；
它和面板同一个 Tk、同一个主循环，面板在它上面，面板隐藏时它清空。

面板右上角贴着拖动标签（overlay.grip，第三个 Toplevel，不穿透、不抢焦点）：按住它把面板拖走，
松手后按炉石客户区的比例记在 overlay-position.json（overlay.position），双击恢复默认位置。
面板和标签合起来叫"箱子"，标签在箱子右上角、面板在它下面，两者不重叠。

只在主线程使用。定时器回调里的任何异常都只记日志，定时器照常继续；高亮出错只影响高亮，
标签出错只是不能拖。
"""

from __future__ import annotations

import logging
import sys
import time
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Protocol

from lscs.config import HighlightConfig, OverlayConfig
from lscs.overlay import geometry, position, win32
from lscs.overlay.banner import Banner
from lscs.overlay.geometry import Anchor, Drag, Layout, Rect
from lscs.overlay.grip import GripWindow
from lscs.overlay.model import (
    CORE_LABEL,
    DIRECTION,
    GOAL,
    PLAN,
    REASON,
    SOURCE_RULES,
    STRONGEST,
    WHY,
    Block,
    Line,
    NextStep,
    OverlayModel,
    OverlayView,
    Target,
    condense,
    reason_lines,
)
from lscs.overlay.theme import (
    AI_TAG,
    BG,
    CORE_TAG,
    DIM,
    DIVIDER,
    MUTED,
    NOW_BG,
    OFFSCREEN,
    OPP_TAG,
    REVIEW_TAG,
    RULES_TAG,
    TEXT,
    TITLE,
    WARN,
)

log = logging.getLogger(__name__)

RENDER_MS = 150  # 读取数据模型的间隔
CONDENSE_LEVELS = 4  # 0 原样，1~3 逐级精简（见 model.condense）
NEXT_SCALE = 1.4  # "下一步"的大字比正文大这么多
SMALL_SCALE = 0.85  # 页眉、来源和小字块
NOW_PAD = 0.3  # 详细模式"现在"块上下各留几个字高
DIVIDER_PAD = 0.25  # 分隔线上下各留几个字高
PLACE_MS = 500  # 重新定位、判断显示/隐藏的间隔
TONES = {"text": TEXT, "dim": DIM, "muted": MUTED}
# 精简模式行首标签的颜色；其余标签（然后、护甲、名次、选项标题）是灰色
LABEL_COLORS = {
    GOAL: AI_TAG,
    PLAN: AI_TAG,
    REASON: AI_TAG,
    DIRECTION: RULES_TAG,
    WHY: RULES_TAG,
    STRONGEST: OPP_TAG,
    CORE_LABEL: CORE_TAG,
}
# 详细模式块标签的颜色
BLOCK_COLORS = {
    "plan": RULES_TAG,
    "choice": RULES_TAG,
    "heroes": RULES_TAG,
    "aside": OPP_TAG,
    "opponents": OPP_TAG,
    "comments": AI_TAG,
    "review": REVIEW_TAG,
}
FAMILIES = {"win32": "Microsoft YaHei UI", "darwin": "PingFang SC"}
SIDES = {"right": "右下角", "left": "左下角", "top": "顶部"}
DOCK_FAILURE_LIMIT = 5  # 贴靠炉石连续失败几次后改用普通置顶窗口
FALLBACK_HINT = "面板已移到屏幕右上角；不需要面板可以用 --no-overlay 关闭"
HIGHLIGHT_ON = "游戏里会用金色框标出下一步要点的牌或按钮。"
BANNER_ON = "战斗一开始棋盘顶部会显示这场的真实结果。"
HIGHLIGHT_OFF_PLAIN = "画面高亮已关闭：面板无法贴靠炉石"
HIGHLIGHT_ONLY_WINDOWS = "画面高亮只在 Windows 上显示。"
DRAGGED_HERE = '显示在上次拖到的位置（双击面板上的"≡ 拖动"恢复默认）'
DRAGGED_HERE_NO_GRIP = "显示在上次拖到的位置（删掉项目文件夹里的 overlay-position.json 恢复默认）"
DRAG_PREVIEW = '可以用右上角的"≡ 拖动"标签拖，位置不保存。'
FOCUS_LOST = (
    "注意：炉石失去了焦点（前台是 lscs 的面板）；"
    "如果反复出现，请把 [overlay] drag_handle 设为 false 并告诉开发者"
)
# 拖动标签已经关掉了：问题不在标签上，只请用户告诉开发者
FOCUS_LOST_NO_GRIP = "注意：炉石失去了焦点（前台是 lscs 的面板）；如果反复出现，请告诉开发者"

Output = Callable[[str], None]
HighlightFactory = Callable[[tk.Misc, HighlightConfig], "HighlightLike"]


class HighlightLike(Protocol):
    @property
    def hwnd(self) -> int | None: ...  # Windows 上的窗口句柄（预览时为 None）

    def show(
        self,
        step: NextStep | None,
        client: Rect | None,
        banner: Banner | None = None,
        markers: Sequence[Target] = (),
    ) -> bool | None:
        """markers：商店核心卡的槽位（画"核"字徽章）。返回 True 表示 Tk 重建了高亮的顶层窗口
        （新的句柄）：面板要重新放到它上面。"""

    def destroy(self) -> None: ...


def highlight_inputs(
    view: OverlayView | None, *, steps: bool, banner: bool, now: float, core: bool = False
) -> tuple[NextStep | None, Banner | None, tuple[Target, ...]]:
    """交给画面高亮的 (下一步, 横幅, 核心卡的槽位)：steps 是 [highlight] enabled，banner 是
    [combat_result] enabled，core 是 [highlight] enabled 且 core_marks；对局结束那场的横幅过了
    expires_at 就不画（定位每 500 毫秒一次，最多晚半秒清掉）。"""
    if view is None:
        return None, None, ()
    shown = view.banner if banner else None
    if shown is not None and shown.expires_at is not None and now >= shown.expires_at:
        shown = None
    return (view.next_step if steps else None), shown, (view.core_targets if core else ())


def _tk_highlight(root: tk.Misc, cfg: HighlightConfig) -> HighlightLike:
    from lscs.overlay.highlight import HighlightWindow

    return HighlightWindow(root, cfg)


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
    def __init__(
        self,
        cfg: OverlayConfig,
        out: Output = _print,
        highlight: HighlightConfig | None = None,
        make_highlight: HighlightFactory = _tk_highlight,
        position_path: Path | None = None,
        banner: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """highlight：config.toml 的 [highlight]；None 时不开画面高亮窗口。
        banner：在高亮窗口上画战斗结果的横幅（config.toml 的 [combat_result]）。[highlight] 关掉
        但要横幅时照样开高亮窗口，只是不画"下一步"；两个都关才不开。clock 判断横幅到期没有。
        position_path：拖到的位置存在哪（lscs watch 用 position.DEFAULT_PATH）；None 时不读不写，
        拖动只在这次有效。"""
        self._cfg = cfg
        self._out = out
        self._position_path = position_path
        self._steps = highlight is not None and highlight.enabled  # 画"下一步"
        # 画商店核心卡的"核"字徽章（[highlight] core_marks；关掉高亮时也不画）
        self._core = self._steps and highlight is not None and highlight.core_marks
        self._banner = highlight is not None and banner  # 画战斗结果的横幅
        self._highlight_cfg = highlight if self._steps or self._banner else None
        self._clock = clock
        self._make_highlight = make_highlight
        self._highlight: HighlightLike | None = None
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
        self._grip: GripWindow | None = None
        self._drag: Drag | None = None  # 正在拖动时：按下时鼠标和箱子的位置
        self._mouse = (0, 0)  # 拖动中鼠标现在的位置（屏幕坐标）
        self._motions = 0  # 收到过几次拖动的移动事件
        self._motions_checked = 0  # 定位的定时器上次看到的 _motions
        self._placed: tuple[Rect, bool, Rect] | None = None  # (参照区域, 是否贴靠炉石, 箱子)
        self._remembered = False  # 已经告诉过玩家"位置已记住"
        self._focus_warned = False  # 已经提醒过"炉石失去了焦点"（每次运行只提醒一次）
        # 贴靠炉石时的锚点相对客户区，退路和 Mac 上的相对屏幕，两者不混用；None 表示用预设
        saved = position.load(self._position_path) if self._position_path is not None else None
        self._anchors: dict[bool, Anchor | None] = {True: saved, False: None}
        # 让 Tk 创建顶层窗口，才能拿到句柄。Tk 只在第一次显示时才创建它，所以没法在显示之前
        # 加上"不抢焦点"的样式；显示后如果抢走了 PowerShell 的焦点，下面再还回去。
        root.update()
        side = SIDES[cfg.position]
        if not win32.IS_WINDOWS:
            root.attributes("-alpha", cfg.opacity)
            self.status = "悬浮面板已打开（不是 Windows，放在屏幕右上角，不穿透鼠标）。"
            self.status += self._open_grip()
            if self._grip is not None:
                self.status += DRAG_PREVIEW
            if self._highlight_cfg is not None:
                self.status += HIGHLIGHT_ONLY_WINDOWS
            return
        self._locator = win32.HearthstoneLocator()
        try:
            self._apply_style()
            self._native = True
            if self._anchors[True] is None:
                where = f"显示在游戏窗口{side}"
            else:
                where = DRAGGED_HERE if cfg.drag_handle else DRAGGED_HERE_NO_GRIP
            self.status = f"悬浮面板已打开：炉石在前台时{where}，切到别的窗口时自动隐藏。"
            self.status += self._open_highlight()
        except Exception as exc:
            log.warning("设置面板窗口样式失败", exc_info=True)
            self._use_plain_window()
            self.status = (
                f"无法让悬浮面板贴靠炉石窗口、穿透鼠标（{_reason(exc)}），{FALLBACK_HINT}。"
                "可以运行 uv run lscs overlay-check 查看详情。"
            )
            if self._highlight_cfg is not None:
                self.status += HIGHLIGHT_OFF_PLAIN + "。"
        self.status += self._open_grip()
        self._give_focus_back(previous)

    def _open_highlight(self) -> str:
        """创建画面高亮窗口，返回加在状态行后面的说明。失败只关掉高亮，面板照常。"""
        if self._highlight_cfg is None:
            return ""
        try:
            self._highlight = self._make_highlight(self._root, self._highlight_cfg)
            self._root.lift()  # 面板永远在高亮之上
        except Exception as exc:
            log.warning("画面高亮无法开启", exc_info=True)
            self._highlight = None
            return f"画面高亮无法开启（{_reason(exc)}），只显示面板。"
        return (HIGHLIGHT_ON if self._steps else "") + (BANNER_ON if self._banner else "")

    def _open_grip(self) -> str:
        """创建拖动标签（放在屏幕外），返回加在状态行后面的说明（成功时为空）。
        失败只是不能拖，面板照常。[overlay] drag_handle = false 时不开。"""
        if not self._cfg.drag_handle:
            return ""
        try:
            self._grip = GripWindow(
                self._root,
                self._font(geometry.font_px(self._screen().height, self._cfg.font_size)),
                self._cfg.opacity,
                self._native,
                on_press=self._grip_press,
                on_motion=self._grip_motion,
                on_release=self._grip_release,
                on_reset=self._grip_reset,
            )
        except Exception as exc:
            log.warning("拖动标签无法开启", exc_info=True)
            self._grip = None
            note = f"面板不能拖动（{_reason(exc)}）。"
            if self._anchors[True] is not None:
                note += "想回默认位置就删掉项目文件夹里的 overlay-position.json。"
            return note
        return ""

    def _close_grip(self, exc: BaseException) -> None:
        """标签出错：关掉它（不能拖了），面板照常。"""
        grip, self._grip = self._grip, None
        self._drag = None
        log.warning("拖动标签出错，已关闭", exc_info=exc)
        if grip is not None:
            grip.destroy()
        self._out(f"拖动标签出错（{_reason(exc)}），面板不能拖动了，其余照常。")

    def _lift_panel(self) -> None:
        """面板和标签都放到画面高亮上面（两者不重叠，谁上谁下无所谓）。"""
        self._root.lift()
        if self._grip is not None:
            self._grip.lift()

    def _close_highlight(self) -> None:
        highlight, self._highlight = self._highlight, None
        if highlight is None:
            return
        try:
            highlight.destroy()
        except Exception:
            log.debug("关闭画面高亮时出错", exc_info=True)

    def _update_highlight(self, client: Rect | None) -> None:
        """client 为 None：面板隐藏了，高亮也清空。画错不影响面板，同一种错误只记一次。"""
        if self._highlight is None:
            return
        step, banner, markers = highlight_inputs(
            self._view, steps=self._steps, banner=self._banner, now=self._clock(), core=self._core
        )
        try:
            if self._highlight.show(step, client, banner, markers):
                self._lift_panel()  # 高亮换了新的顶层窗口：面板和标签重新放到它上面
        except Exception:
            self._report("画面高亮出错")

    def _give_focus_back(self, previous: int | None) -> None:
        """面板（或画面高亮、拖动标签）第一次显示时可能抢走 PowerShell 的焦点
        （之后按 Ctrl+C 没反应），还给它。"""
        try:
            panel = win32.root_window(self._root.winfo_id())
            highlight = self._highlight.hwnd if self._highlight is not None else None
            grip = self._grip.hwnd if self._grip is not None else None
            ours = {hwnd for hwnd in (panel, highlight, grip) if hwnd}
            if previous and ours and previous not in ours and win32.foreground_window() in ours:
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
        self._close_highlight()
        grip, self._grip = self._grip, None
        if grip is not None:
            grip.destroy()  # 先于 root：标签的绑定引用着 self
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
            self._end_lost_drag()
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
        """退回普通置顶窗口（屏幕右上角）：标签也一样；拖到的位置改用相对屏幕的那一份。"""
        self._native = False
        self._root.attributes("-alpha", self._cfg.opacity)
        self._visible = True
        if self._grip is not None:
            try:
                self._grip.use_plain()
            except Exception as exc:
                self._close_grip(exc)
        if self._highlight is not None:  # 普通置顶窗口没法保证穿透：高亮不画了
            self._close_highlight()
            self._out(HIGHLIGHT_OFF_PLAIN)

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
        if hearthstone is not None and foreground in self._own_windows():
            self._note_focus_lost()  # 面板和标签都不该成为前台：提醒一次，方便排查
        # 高亮窗口偶尔在前台（比如刚创建时）也算：不然面板会隐藏一个周期
        highlight = self._highlight.hwnd if self._highlight is not None else None
        grip = self._grip.hwnd if self._grip is not None else None
        if not win32.should_show(hearthstone, foreground, self._hwnd, minimized, highlight, grip):
            return None
        client = win32.client_rect(hearthstone)
        return (client, True) if client is not None else None

    def _place(self) -> None:
        target = self._target()
        if target is None:
            self._placed = None
            self._update_highlight(None)  # 先清空高亮：它从不抛出异常
            self._check_grip_style()  # 隐藏时也检查（重新加样式会把它显示出来，下一行再藏好）
            self._set_visible(False)
            return
        area, docked = target
        layout = geometry.panel_layout(area, self._cfg.font_size)
        if layout != self._layout:
            self._layout = layout
            self._draw()
            if self._grip is not None:
                self._grip.set_font(self._font(layout.font_px))
        panel_w, panel_h = self._size
        grip_w, grip_h = geometry.grip_size(layout) if self._grip is not None else (0, 0)
        # 箱子 = 标签 + 面板；面板只有一行变窄时也不窄于标签
        width, height = max(panel_w, grip_w), panel_h + grip_h
        if docked:
            screen = (win32.virtual_screen() if self._native else None) or self._screen()
        else:
            screen = area  # 参照区域就是屏幕
        if self._drag is not None:  # 拖动中：跟着鼠标，但不出客户区和屏幕
            x, y = self._drag.moved_to(*self._mouse)
            x, y = geometry.keep_inside(x, y, width, height, area, screen)
        else:
            preset = geometry.PRESETS[self._cfg.position] if docked else geometry.CORNER
            x, y = geometry.dock(area, screen, width, height, self._anchors[docked] or preset)
        panel, grip = geometry.split_box(x, y, width, panel_h, grip_w, grip_h)
        if (panel.width, panel.height, panel.x, panel.y) != self._geometry:
            self._geometry = (panel.width, panel.height, panel.x, panel.y)
            self._root.geometry(f"{panel.width}x{panel.height}+{panel.x}+{panel.y}")
        self._placed = (area, docked, Rect(x, y, width, height))
        self._place_grip(grip)
        self._set_visible(True)
        self._update_highlight(area if docked else None)

    def _check_grip_style(self) -> bool:
        """Windows 上每次定位（显示或隐藏）都检查标签的样式；Tk 重建了它的顶层窗口时把面板和它
        放回高亮上面。返回 False 表示没有标签（出错时已经关掉了）。"""
        if self._grip is None:
            return False
        try:
            if self._grip.apply_style():
                self._lift_panel()
        except Exception as exc:
            self._close_grip(exc)
            return False
        return True

    def _place_grip(self, rect: Rect) -> None:
        if not self._check_grip_style():
            return
        try:
            self._grip.place(rect)
        except Exception as exc:
            self._close_grip(exc)

    def _set_visible(self, visible: bool) -> None:
        """只有贴靠炉石时才会隐藏，标签跟着面板一起。隐藏用透明度 0 而不是 withdraw()：窗口
        一直在，不会因为重新显示而抢走炉石的焦点，Tk 也不会重建顶层窗口、丢掉穿透样式；
        标签隐藏时还移到屏幕外，不会挡住别的程序（_place 先放回原位再显示）。
        标签出错只关掉标签。"""
        if not self._native or self._hwnd is None:
            return
        if visible != self._visible:
            if not win32.set_opacity(self._hwnd, self._cfg.opacity if visible else 0.0):
                raise RuntimeError("无法设置面板的透明度")
            self._visible = visible
        if self._grip is not None:
            try:
                self._grip.set_visible(visible)
            except Exception as exc:
                self._close_grip(exc)

    # ---- 拖动（标签的回调，屏幕坐标） ----

    def _own_windows(self) -> set[int]:
        """面板和拖动标签的句柄。"""
        grip = self._grip.hwnd if self._grip is not None else None
        return {hwnd for hwnd in (self._hwnd, grip) if hwnd}

    def _note_focus_lost(self) -> None:
        if self._focus_warned:
            return
        self._focus_warned = True
        log.warning("炉石失去了焦点：前台是 lscs 的面板或拖动标签")
        self._out(FOCUS_LOST if self._cfg.drag_handle else FOCUS_LOST_NO_GRIP)

    def _keep_hearthstone_in_front(self) -> None:
        """标签不抢焦点（WS_EX_NOACTIVATE）；万一按下、松开时前台还是成了面板或标签，马上还给
        炉石，并提醒一次。"""
        if not self._native:
            return
        try:
            if win32.foreground_window() not in self._own_windows():
                return
            hearthstone = self._locator.get()
            if hearthstone is None:
                return
            self._note_focus_lost()
            win32.set_foreground(hearthstone)
        except Exception:
            log.debug("把焦点还给炉石时出错", exc_info=True)

    def _grip_press(self, x: int, y: int) -> None:
        self._keep_hearthstone_in_front()
        if self._placed is None:  # 面板隐藏着（标签透明，鼠标本来就穿过去）
            return
        box = self._placed[2]
        self._drag = Drag(x, y, box.x, box.y)
        self._mouse = (x, y)

    def _grip_motion(self, x: int, y: int) -> None:
        if self._drag is None:
            return
        self._mouse = (x, y)
        self._motions += 1
        self._place_now()  # 立刻跟手，不等 500 毫秒

    def _grip_release(self, x: int, y: int) -> None:
        """松手：拖够了就记住位置（贴靠炉石时写进文件）；只是点了一下就回到原位。
        炉石这时不在前台（面板隐藏着）就什么都不记。"""
        self._keep_hearthstone_in_front()
        drag = self._drag
        if drag is None:
            return
        self._mouse = (x, y)
        moved = drag.far_enough(x, y)
        if moved:
            self._place_now()  # 跟到松手的位置
        self._drag = None
        if not moved:
            self._place_now()  # 回到原位
            return
        if self._placed is None:
            return
        area, docked, box = self._placed
        anchor = geometry.anchor_from_rect(box, area)
        self._anchors[docked] = anchor
        if docked and self._position_path is not None:
            self._save_position(self._position_path, anchor)

    def _end_lost_drag(self) -> None:
        """拖动中松手的消息没收到（鼠标捕获被别的窗口拿走了，比如弹出了系统对话框）：
        鼠标主键其实已经松开、而且上次检查以来没有移动事件，就当作在最后的位置松手。
        只有 Windows 上知道鼠标键的状态；其余平台等真的松手。"""
        if self._drag is None:
            return
        moved_since_check = self._motions != self._motions_checked
        self._motions_checked = self._motions
        if moved_since_check or win32.primary_button_down() is not False:
            return
        log.info("拖动标签没收到松手的消息，按最后的位置松手")
        self._grip_release(*self._mouse)

    def _save_position(self, path: Path, anchor: Anchor) -> None:
        try:
            position.save(path, anchor)
        except OSError as exc:
            self._report("保存面板位置时出错")
            self._out(f"面板位置无法保存：{_reason(exc)}")
            return
        if not self._remembered:
            self._remembered = True
            self._out(f'面板位置已记住（{path.name}）；双击"≡ 拖动"恢复默认位置')

    def _grip_reset(self) -> None:
        """双击标签：回到预设位置。双击的第二下 Tk 只发 Double-Button-1，不会开始新的拖动；
        万一先开始了，这里也取消掉，随后的松手看到没在拖就什么都不做。"""
        self._drag = None
        docked = self._native  # 显示着的时候，贴靠炉石当且仅当用的是 Windows 窗口样式
        self._anchors[docked] = None
        if docked and self._position_path is not None:
            try:
                position.clear(self._position_path)
            except OSError as exc:
                self._report("删除面板位置文件时出错")
                self._out(f"面板位置文件无法删除：{_reason(exc)}")
        self._place_now()
        self._out("面板位置已恢复默认")

    def _place_now(self) -> None:
        """拖动时马上定位。拖动一下就有几十个移动事件：出错和定时器一样只记一次。"""
        try:
            self._place()
        except Exception:
            self._report("移动悬浮面板时出错")

    # ---- 绘制 ----

    def _font(self, px: int, bold: bool = False) -> tkfont.Font:
        key = (px, bold)
        if key not in self._fonts:
            weight = "bold" if bold else "normal"
            self._fonts[key] = tkfont.Font(
                root=self._root, family=self._family, size=-px, weight=weight
            )
        return self._fonts[key]

    def _label(
        self, parent: tk.Misc, text: str, color: str, font: tkfont.Font, bg: str = BG
    ) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            fg=color,
            bg=bg,
            font=font,
            justify="left",
            anchor="w",
            padx=0,
            pady=0,
            borderwidth=0,
        )

    def _draw(self) -> None:
        """画出当前内容；超过最大高度时逐级精简（见 model.condense），最后仍然超高就截掉底部。"""
        view, layout = self._view, self._layout
        if view is None or layout is None:
            return
        height = 0
        for level in range(CONDENSE_LEVELS):
            height = self._draw_level(condense(view, level, self._cfg.detail), layout, level)
            if height <= layout.max_height or view.compact:
                break
        width = layout.width
        if view.compact:  # 只有一行时按文字宽度收窄
            bold = self._font(layout.font_px, bold=True)
            width = min(layout.width, bold.measure(view.title) + 2 * layout.pad + 2)
        self._size = (max(width, 1), max(min(height, layout.max_height), 1))

    def _draw_level(self, view: OverlayView, layout: Layout, level: int) -> int:
        """重建所有标签，返回需要的高度。从上到下：页眉（有"下一步"时是灰色小字，右边是来源）
        或粗体标题、"下一步"（详细模式包在带底色的"现在"块里）、各块（详细模式每块前面一条
        分隔线和块标签）、提示、警告。"""
        for child in self._frame.winfo_children():
            child.destroy()
        self._frame.configure(padx=layout.pad, pady=layout.pad)
        detail = self._cfg.detail
        draw = _Drawing(self, layout)
        step = view.next_step if not view.compact else None
        if step is None:
            draw.text(self._frame, view.title, TITLE, draw.bold)
        else:
            draw.header(view)
            if detail:
                draw.now(step, reason_lines(level, detail=True))
            else:
                draw.next_step(step, reason_lines(level))
        if not view.compact:
            for block in view.blocks:
                if detail:
                    draw.section(block)
                else:
                    draw.block(block)
            if detail and (view.notice or view.warning):
                draw.divider()
            for text, color in ((view.notice, MUTED), (view.warning, WARN)):
                if text:
                    draw.text(self._frame, text, color, draw.regular, top=draw.gap // 2)
        self._root.update_idletasks()
        return self._frame.winfo_reqheight()

    # ---- 冒烟测试用 ----

    def texts(self) -> list[str]:
        """面板上所有文字标签的内容，从上到下、从左到右（分隔线、底色块不算）。"""

        def walk(widget: tk.Misc) -> Iterator[str]:
            for child in widget.winfo_children():
                if isinstance(child, tk.Label):
                    yield child.cget("text")
                else:
                    yield from walk(child)

        return list(walk(self._frame))

    @property
    def size(self) -> tuple[int, int]:
        return self._size


def _wrapped(text: str, width: int, font: tkfont.Font, max_lines: int = 0) -> str:
    """按像素宽度折行；max_lines 不为 0 时只留这么多行，截掉的末尾加"…"。"""
    lines = geometry.wrap(text, max(width, 1), font.measure)
    if max_lines:
        lines = geometry.clip(lines, max_lines, max(width, 1), font.measure)
    return "\n".join(lines)


class _Drawing:
    """画一层内容（_draw_level 每试一级精简就画一次）：字体、宽度和各种行的画法。"""

    def __init__(self, window: OverlayWindow, layout: Layout) -> None:
        self._window = window
        px = self.px = layout.font_px
        small = round(px * SMALL_SCALE)
        self.regular = window._font(px)
        self.bold = window._font(px, bold=True)
        self.small = window._font(small)
        self.small_bold = window._font(small, bold=True)
        self.big = window._font(round(px * NEXT_SCALE), bold=True)
        self.inner = layout.width - 2 * layout.pad
        self.gap = max(4, px // 2)
        self.space = max(4, round(px * 0.4))  # 行首标签和正文之间

    def label(
        self, parent: tk.Misc, text: str, color: str, font: tkfont.Font, bg: str = BG
    ) -> tk.Label:
        return self._window._label(parent, text, color, font, bg)

    def text(
        self,
        parent: tk.Misc,
        text: str,
        color: str,
        font: tkfont.Font,
        *,
        width: int | None = None,
        max_lines: int = 0,
        top: int = 0,
        bg: str = BG,
    ) -> tk.Label:
        """一段折好行的文字，占一整行。"""
        wrapped = _wrapped(text, self.inner if width is None else width, font, max_lines)
        label = self.label(parent, wrapped, color, font, bg)
        label.pack(anchor="w", pady=(top, 0))
        return label

    def header(self, view: OverlayView) -> None:
        """页眉：左边回合 · 铸币 · 本（灰色小字），右边来源（彩色粗体）和后面的灰字。"""
        row = tk.Frame(self._window._frame, bg=BG)
        row.pack(fill="x", anchor="w")
        row.grid_columnconfigure(0, weight=1)
        right = 0
        if view.source:
            right += self.small_bold.measure(view.source)
        if view.source_note:
            right += self.small.measure(" " + view.source_note)
        width = self.inner - right - (self.gap if right else 0)
        title = self.label(row, _wrapped(view.title, width, self.small), MUTED, self.small)
        title.grid(row=0, column=0, sticky="nw")
        if view.source:
            color = RULES_TAG if view.source == SOURCE_RULES else AI_TAG
            source = self.label(row, view.source, color, self.small_bold)
            source.grid(row=0, column=1, sticky="ne")
        if view.source_note:
            note = self.label(row, view.source_note, MUTED, self.small)
            note.grid(row=0, column=2, sticky="ne", padx=(self.space // 2 if view.source else 0, 0))

    def next_step(self, step: NextStep, reason_max: int) -> None:
        """画"下一步"：大字，下面小圆点；有理由时一行"理由 ……"（站位是"为何"）。等待之类的
        状态是灰色大字。"""
        color = MUTED if step.tone == "muted" else TEXT
        parent = self._window._frame
        self.text(parent, step.text, color, self.big, top=self.gap // 2)
        if step.slots:
            self.text(parent, step.slots, color, self.regular)
        if step.reason:
            line = Line(step.reason, label=step.reason_label, max_lines=reason_max)
            self.labeled(parent, line, self.regular)

    def labeled(self, parent: tk.Misc, line: Line, font: tkfont.Font, top: int = 0) -> None:
        """精简模式的一行：粗体标签，后面是折好行的正文（折行时和正文第一行对齐）。"""
        color = TONES[line.tone]
        if not line.label:
            self.text(parent, line.text, color, font, max_lines=line.max_lines, top=top)
            return
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", anchor="w", pady=(top, 0))
        tag = self.label(row, line.label, LABEL_COLORS.get(line.label, MUTED), self.bold)
        tag.pack(side="left", anchor="nw")
        width = self.inner - self.bold.measure(line.label) - self.space
        body = self.label(row, _wrapped(line.text, width, font, line.max_lines), color, font)
        body.pack(side="left", anchor="nw", padx=(self.space, 0))

    def block(self, block: Block) -> None:
        font = self.small if block.small else self.regular
        for i, line in enumerate(block.lines):
            self.labeled(self._window._frame, line, font, top=self.gap // 2 if i == 0 else 0)

    # ---- 详细模式 ----

    def now(self, step: NextStep, reason_max: int) -> None:
        """画"现在"块：带底色（直角），里面是"下一步"的大字、小圆点和灰色的理由（不带标签）。"""
        pad_x, pad_y = self.space, round(self.px * NOW_PAD)
        box = tk.Frame(self._window._frame, bg=NOW_BG, padx=pad_x, pady=pad_y)
        box.pack(fill="x", anchor="w", pady=(self.gap // 2, 0))
        width = self.inner - 2 * pad_x
        color = MUTED if step.tone == "muted" else TEXT
        self.text(box, step.text, color, self.big, width=width, bg=NOW_BG)
        if step.slots:
            self.text(box, step.slots, color, self.regular, width=width, bg=NOW_BG)
        if step.reason:
            self.text(
                box, step.reason, DIM, self.regular, width=width, max_lines=reason_max, bg=NOW_BG
            )

    def divider(self) -> None:
        """块与块之间一条 1 像素的分隔线。"""
        pad = round(self.px * DIVIDER_PAD)
        line = tk.Frame(self._window._frame, bg=DIVIDER, height=1)
        line.pack(fill="x", pady=(pad + self.gap // 2, pad))

    def section(self, block: Block) -> None:
        """详细模式的一块：分隔线，块标签（旁边灰色小字的附注），块内每行缩进一个字宽；
        有右列（统计、护甲）的行右边对齐一段灰色小字。"""
        parent = self._window._frame
        self.divider()
        head = tk.Frame(parent, bg=BG)
        head.pack(fill="x", anchor="w")
        tag = self.label(head, block.label, BLOCK_COLORS.get(block.kind, MUTED), self.bold)
        tag.pack(side="left", anchor="sw")
        if block.note:
            width = self.inner - self.bold.measure(block.label) - self.space
            note = self.label(head, _wrapped(block.note, width, self.small), MUTED, self.small)
            note.pack(side="left", anchor="sw", padx=(self.space, 0))
        indent = self.px
        for line in block.lines:
            font = self.small if block.small or line.small else self.regular
            color = TONES[line.tone]
            row = tk.Frame(parent, bg=BG)
            row.pack(fill="x", anchor="w", padx=(indent, 0))
            width = self.inner - indent
            if line.right:
                width -= self.small.measure(line.right) + self.space
            body = self.label(row, _wrapped(line.text, width, font, line.max_lines), color, font)
            body.pack(side="left", anchor="nw")
            if line.right:
                right = self.label(row, line.right, MUTED, self.small)
                right.pack(side="right", anchor="ne")
