"""lscs overlay-check：逐项检查悬浮面板在这台 Windows 电脑上能不能贴靠炉石、穿透鼠标。

每一步打印一行"√/× 步骤：说明"（"!" 表示要注意、但不影响面板）。关键步骤都通过时返回 True。
最后一步检查面板右上角的拖动标签（不穿透、不抢焦点），它失败时面板照常，只是不能拖。
这些符号在中文 Windows 控制台（GBK、默认字体）里都能显示。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from lscs.config import OverlayConfig
from lscs.overlay import win32
from lscs.overlay.geometry import Rect

log = logging.getLogger(__name__)

Output = Callable[[str], None]
GRIP_STEP = "拖动标签"  # 不是关键步骤：失败时面板照常，只是不能拖
PANEL_STEPS = (
    "取得面板窗口句柄",
    "设置鼠标穿透和半透明",
    "检查窗口样式",
    "隐藏和重新显示",
    GRIP_STEP,
)
OFFSCREEN = "1x1+-32000+-32000"  # 放在屏幕外，检查时看不到


def _print(line: str) -> None:
    print(line, flush=True)


def _tk_root() -> Any:
    import tkinter as tk

    root = tk.Tk()
    try:
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(OFFSCREEN)
        root.update()  # 让 Tk 创建顶层窗口
    except BaseException:
        root.destroy()
        raise
    return root


def _tk_grip(root: Any) -> Any:
    """和面板的拖动标签一样：同一个 Tk 的第二个顶层窗口。"""
    import tkinter as tk

    top = tk.Toplevel(root)
    try:
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.geometry(OFFSCREEN)
        top.update()
    except BaseException:
        top.destroy()
        raise
    return top


def _reason(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


class Note(Exception):
    """要提醒用户、但不算关键失败的情况（比如炉石正好最小化了）。"""


class _Checker:
    def __init__(self, out: Output) -> None:
        self._out = out
        self.ok = True

    def step(self, name: str, critical: bool, run: Callable[[], str]) -> bool:
        try:
            detail = run()
        except Note as note:
            self._out(f"! {name}：{note}")
            return False
        except Exception as exc:
            log.debug("overlay-check：%s 失败", name, exc_info=True)
            self._out(f"× {name}：{_reason(exc)}")
            self.ok = self.ok and not critical
            return False
        self._out(f"√ {name}：{detail}")
        return True


def _load_api() -> str:
    win32.load_api()
    return "user32、kernel32 已加载"


def _dpi() -> str:
    method = win32.set_dpi_awareness()
    if not method:
        raise RuntimeError(
            "SetProcessDpiAwareness 和 SetProcessDPIAware 都失败，面板位置可能有偏差"
        )
    return f"已设置（{method}）"


def _screen() -> str:
    screen = win32.virtual_screen()
    if screen is None:
        raise RuntimeError("GetSystemMetrics 返回 0")
    return f"{screen.width}×{screen.height}，左上角 ({screen.x}, {screen.y})"


MINIMIZED = "炉石现在是最小化状态：请切回炉石（或把炉石改成窗口模式）后再运行一次"


def locate_hearthstone() -> tuple[int, Rect | None]:
    """炉石主窗口的句柄和客户区（取不到时为 None）。最小化时抛出 Note，没找到时抛出
    RuntimeError，说明都是给玩家看的（overlay-check 和 highlight-test 共用）。"""
    hwnd = win32.find_hearthstone()
    if hwnd is None:
        if win32.hearthstone_windows():
            # 有炉石的 Unity 窗口，只是客户区是 0×0：全屏的炉石切到 PowerShell 时会自动最小化
            raise Note(MINIMIZED)
        others = [
            cls
            for _hwnd, cls, _area in win32.hearthstone_windows(require_unity=False)
            if cls != win32.UNITY_CLASS
        ]
        if others:
            classes = "、".join(sorted({cls or "（无）" for cls in others}))
            raise RuntimeError(
                f"Hearthstone.exe 有窗口，但窗口类名是 {classes}，不是 {win32.UNITY_CLASS}"
            )
        raise RuntimeError("没找到炉石的窗口：请先打开炉石再运行")
    return hwnd, win32.client_rect(hwnd)


def _hearthstone() -> str:
    _hwnd, rect = locate_hearthstone()
    if rect is None:
        return "找到了，但取不到客户区位置（炉石可能最小化了）"
    return f"找到了，客户区 {rect.width}×{rect.height}，位置 ({rect.x}, {rect.y})"


def run_check(
    cfg: OverlayConfig, out: Output = _print, make_root: Callable[[], Any] = _tk_root
) -> bool:
    if not win32.IS_WINDOWS:
        out("× 不是 Windows：贴靠炉石窗口和鼠标穿透只在 Windows 上可用（面板会固定在屏幕右上角）。")
        return False
    check = _Checker(out)
    if not check.step("加载 Win32 API", True, _load_api):
        out("Win32 API 无法加载，其余检查跳过。")
        return False
    check.step("DPI 感知", False, _dpi)  # 必须在创建 Tk 窗口之前
    check.step("虚拟屏幕", False, _screen)
    check.step("找炉石窗口", True, _hearthstone)

    holder: dict[str, Any] = {}

    def create() -> str:
        holder["root"] = make_root()
        return "已创建（放在屏幕外）"

    if check.step("创建面板窗口", True, create):
        try:
            _check_panel(check, cfg, holder["root"], out)
        finally:
            holder["root"].destroy()
    else:
        out(f"跳过：{'、'.join(PANEL_STEPS)}")
    if check.ok:
        out("检查通过：悬浮面板应该可以正常贴在炉石上、穿透鼠标。")
    else:
        out(
            "有关键项目失败：watch 时面板会退回屏幕右上角的普通置顶窗口"
            "（或用 --no-overlay 只用终端）。请把上面的输出发给开发者。"
        )
    return check.ok


def _check_panel(check: _Checker, cfg: OverlayConfig, root: Any, out: Output) -> None:
    holder: dict[str, int] = {}

    def handle() -> str:
        hwnd = win32.root_window(root.winfo_id())
        if hwnd is None:
            raise RuntimeError("GetAncestor 返回空")
        holder["hwnd"] = hwnd
        return f"0x{hwnd:X}"

    if not check.step(PANEL_STEPS[0], True, handle):
        out(f"跳过：{'、'.join(PANEL_STEPS[1:])}")
        return
    hwnd = holder["hwnd"]

    def style() -> str:
        win32.make_overlay(hwnd, cfg.opacity, cfg.click_through)
        through = "开" if cfg.click_through else "关（config.toml 里关掉了）"
        return f"已设置（鼠标穿透：{through}，不透明度 {cfg.opacity:g}）"

    def verify() -> str:
        if not win32.has_overlay_style(hwnd, cfg.click_through):
            raise RuntimeError("设置后读回的扩展样式不完整")
        return "分层、穿透、不进任务栏、不抢焦点都已生效"

    def opacity() -> str:
        win32.set_opacity(hwnd, 0.0)
        win32.set_opacity(hwnd, cfg.opacity)
        return "正常"

    check.step(PANEL_STEPS[1], True, style)
    check.step(PANEL_STEPS[2], True, verify)
    check.step(PANEL_STEPS[3], True, opacity)
    check.step(GRIP_STEP, False, lambda: _check_grip(cfg, root))


def _check_grip(cfg: OverlayConfig, root: Any) -> str:
    """拖动标签：分层、不进任务栏、不抢焦点，但不穿透（不然按不住它）。"""
    try:
        top = _tk_grip(root)
        try:
            hwnd = win32.root_window(top.winfo_id())
            if hwnd is None:
                raise RuntimeError("GetAncestor 返回空")
            win32.make_overlay(hwnd, cfg.opacity, False)
            if not win32.has_overlay_style(hwnd, False):
                raise RuntimeError("设置后读回的扩展样式不完整")
        finally:
            top.destroy()
    except Exception as exc:
        raise RuntimeError(f"{_reason(exc)}；面板能用，只是不能拖") from exc
    return "不穿透、不抢焦点、不进任务栏"
