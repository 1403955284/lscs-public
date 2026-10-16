"""Windows 辅助函数：找炉石窗口、取客户区位置、判断前台、让面板和画面高亮鼠标穿透。

只用 ctypes，只在 Windows 上加载（第一次调用时）。非 Windows 上所有函数返回 None / False / ""。
Windows 上：
- 查询失败（比如窗口刚好关闭，API 返回 0）时返回 None / False，下次再试；
- 设置面板样式、透明度失败时抛出 Win32Error，信息里有出错的 Win32 函数名和错误码；
- ctypes 本身出错（加载失败、函数不存在）时抛出原来的异常。
界面遇到异常时退回"屏幕右上角的普通置顶窗口"，并在终端说明原因。
"""

from __future__ import annotations

import logging
import ntpath
import sys
import time
from collections.abc import Callable, Iterable
from types import SimpleNamespace

from lscs.overlay.geometry import Rect

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"
HEARTHSTONE_EXE = "hearthstone.exe"
UNITY_CLASS = "UnityWndClass"  # 炉石（Unity 游戏）主窗口的窗口类名

GA_ROOT = 2
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x80000
WS_EX_TRANSPARENT = 0x20
WS_EX_TOOLWINDOW = 0x80
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 2
# 画面高亮：鼠标穿透、不进任务栏、不抢焦点。分层和色键由 Tk 的 -transparentcolor 设置
PASSTHROUGH_BITS = WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_PER_MONITOR_DPI_AWARE = 2
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_SWAPBUTTON = 23
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02
KEY_DOWN = 0x8000  # GetAsyncKeyState 的最高位：现在按着
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


class Win32Error(OSError):
    """某个 Win32 调用失败。"""

    def __init__(self, step: str, code: int) -> None:
        super().__init__(f"{step} 失败（错误码 {code}）")
        self.step = step
        self.code = code


# ---- 纯函数（任何平台都可以测试） ----


def is_hearthstone_path(path: str) -> bool:
    return ntpath.basename(path).lower() == HEARTHSTONE_EXE


def pick_largest(candidates: Iterable[tuple[int, int]]) -> int | None:
    """(窗口句柄, 面积) 中面积最大的窗口。"""
    best = max(candidates, key=lambda c: c[1], default=None)
    return best[0] if best is not None else None


def overlay_style(current: int, click_through: bool) -> int:
    """面板窗口的扩展样式：分层（半透明）、不进任务栏、不抢焦点，可选鼠标穿透。"""
    style = current | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    return style | WS_EX_TRANSPARENT if click_through else style & ~WS_EX_TRANSPARENT


def should_show(
    hearthstone: int | None,
    foreground: int | None,
    panel: int | None,
    minimized: bool,
    highlight: int | None = None,
    grip: int | None = None,
    others: Iterable[int | None] = (),
) -> bool:
    """炉石（或面板自己、画面高亮窗口、拖动标签、others 里的"对手"标签和卡图窗口）在前台、
    而且没有最小化时才显示。"""
    if hearthstone is None or foreground is None or minimized:
        return False
    return foreground in (hearthstone, panel, highlight, grip, *others)


class HearthstoneLocator:
    """缓存炉石窗口句柄；句柄失效时立即重找，找不到时每 rescan 秒最多找一次。"""

    def __init__(
        self,
        find: Callable[[], int | None] | None = None,
        valid: Callable[[int], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        rescan: float = 2.0,
    ) -> None:
        self._find = find or find_hearthstone
        self._valid = valid or is_window_valid
        self._clock = clock
        self._rescan = rescan
        self._hwnd: int | None = None
        self._last_miss: float | None = None

    def get(self) -> int | None:
        if self._hwnd is not None:
            if self._valid(self._hwnd):
                return self._hwnd
            self._hwnd = None
            self._last_miss = None
        now = self._clock()
        if self._last_miss is not None and now - self._last_miss < self._rescan:
            return None
        self._hwnd = self._find()
        self._last_miss = None if self._hwnd is not None else now
        return self._hwnd


# ---- Win32 调用 ----

_api_cache: SimpleNamespace | None = None


def _load() -> SimpleNamespace:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)  # Windows 8.1 起才有
    except OSError:
        shcore = None

    def sig(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    HWND, BOOL, DWORD = wintypes.HWND, wintypes.BOOL, wintypes.DWORD
    enum_proc = ctypes.WINFUNCTYPE(BOOL, HWND, wintypes.LPARAM)
    return SimpleNamespace(
        ctypes=ctypes,
        wintypes=wintypes,
        get_last_error=ctypes.get_last_error,
        WNDENUMPROC=enum_proc,
        EnumWindows=sig(user32.EnumWindows, BOOL, enum_proc, wintypes.LPARAM),
        IsWindow=sig(user32.IsWindow, BOOL, HWND),
        IsWindowVisible=sig(user32.IsWindowVisible, BOOL, HWND),
        IsIconic=sig(user32.IsIconic, BOOL, HWND),
        GetClassNameW=sig(user32.GetClassNameW, ctypes.c_int, HWND, wintypes.LPWSTR, ctypes.c_int),
        GetWindowThreadProcessId=sig(
            user32.GetWindowThreadProcessId, DWORD, HWND, ctypes.POINTER(DWORD)
        ),
        GetClientRect=sig(user32.GetClientRect, BOOL, HWND, ctypes.POINTER(wintypes.RECT)),
        ClientToScreen=sig(user32.ClientToScreen, BOOL, HWND, ctypes.POINTER(wintypes.POINT)),
        GetForegroundWindow=sig(user32.GetForegroundWindow, HWND),
        SetForegroundWindow=sig(user32.SetForegroundWindow, BOOL, HWND),
        GetAncestor=sig(user32.GetAncestor, HWND, HWND, wintypes.UINT),
        GetWindowLongW=sig(user32.GetWindowLongW, wintypes.LONG, HWND, ctypes.c_int),
        SetWindowLongW=sig(user32.SetWindowLongW, wintypes.LONG, HWND, ctypes.c_int, wintypes.LONG),
        SetLayeredWindowAttributes=sig(
            user32.SetLayeredWindowAttributes, BOOL, HWND, wintypes.COLORREF, ctypes.c_ubyte, DWORD
        ),
        GetSystemMetrics=sig(user32.GetSystemMetrics, ctypes.c_int, ctypes.c_int),
        GetAsyncKeyState=sig(user32.GetAsyncKeyState, ctypes.c_short, ctypes.c_int),
        SetProcessDPIAware=sig(user32.SetProcessDPIAware, BOOL),
        SetProcessDpiAwareness=(
            sig(shcore.SetProcessDpiAwareness, ctypes.c_long, ctypes.c_int) if shcore else None
        ),
        OpenProcess=sig(kernel32.OpenProcess, wintypes.HANDLE, DWORD, BOOL, DWORD),
        QueryFullProcessImageNameW=sig(
            kernel32.QueryFullProcessImageNameW,
            BOOL,
            wintypes.HANDLE,
            DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(DWORD),
        ),
        CloseHandle=sig(kernel32.CloseHandle, BOOL, wintypes.HANDLE),
    )


def _api() -> SimpleNamespace:
    global _api_cache
    if _api_cache is None:
        _api_cache = _load()
    return _api_cache


def load_api() -> bool:
    """加载 user32 / kernel32 并设置函数签名；失败时抛出异常。非 Windows 返回 False。"""
    if not IS_WINDOWS:
        return False
    _api()
    return True


def _signed32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value - 0x1_0000_0000 if value & 0x8000_0000 else value


def set_dpi_awareness() -> str:
    """让 Win32 坐标和 Tk 坐标都是物理像素，必须在创建任何窗口之前调用。
    返回成功的那个函数名；都失败（或不是 Windows）时返回空字符串。"""
    if not IS_WINDOWS:
        return ""
    api = _api()
    if api.SetProcessDpiAwareness is not None:
        result = api.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
        if result == 0:
            return "SetProcessDpiAwareness"
        # E_ACCESSDENIED 表示已经设置过（比如程序清单里声明了），再试旧接口
        log.debug("SetProcessDpiAwareness 返回 0x%08X", result & 0xFFFFFFFF)
    return "SetProcessDPIAware" if api.SetProcessDPIAware() else ""


def root_window(widget_id: int) -> int | None:
    """Tk 窗口内部句柄所在的顶层窗口（真正显示在屏幕上的那个）。"""
    if not IS_WINDOWS:
        return None
    return _api().GetAncestor(widget_id, GA_ROOT) or None


def _required_bits(click_through: bool) -> int:
    return overlay_style(0, click_through)


def has_overlay_style(hwnd: int, click_through: bool) -> bool:
    """要求的样式位都在；不穿透时（拖动标签、click_through = false 的面板）还要求穿透位不在，
    不然窗口收不到鼠标。"""
    if not IS_WINDOWS:
        return False
    current = _api().GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    required = _required_bits(click_through)
    if current & required != required:
        return False
    return click_through or not current & WS_EX_TRANSPARENT


def set_opacity(hwnd: int, opacity: float) -> bool:
    """分层窗口的整体不透明度；0 表示完全透明（鼠标也会穿过去），用来隐藏面板。
    失败时抛出 Win32Error。"""
    if not IS_WINDOWS:
        return False
    api = _api()
    alpha = max(0, min(255, round(opacity * 255)))
    if not api.SetLayeredWindowAttributes(hwnd, 0, alpha, LWA_ALPHA):
        raise Win32Error("SetLayeredWindowAttributes", api.get_last_error())
    return True


def make_overlay(hwnd: int, opacity: float, click_through: bool) -> bool:
    """加上面板需要的扩展样式并设置透明度。失败时恢复原样式并抛出 Win32Error。"""
    if not IS_WINDOWS:
        return False
    api = _api()
    old = api.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    api.SetWindowLongW(hwnd, GWL_EXSTYLE, _signed32(overlay_style(old, click_through)))
    try:
        if not has_overlay_style(hwnd, click_through):
            raise Win32Error("SetWindowLongW", api.get_last_error())
        set_opacity(hwnd, opacity)
    except Exception:
        api.SetWindowLongW(hwnd, GWL_EXSTYLE, _signed32(old))
        raise
    return True


def has_passthrough_style(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return False
    current = _api().GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    return current & PASSTHROUGH_BITS == PASSTHROUGH_BITS


def make_passthrough(hwnd: int) -> bool:
    """画面高亮窗口：只加上穿透、不进任务栏、不抢焦点的扩展样式。不调
    SetLayeredWindowAttributes（面板用的 LWA_ALPHA 会盖掉 Tk 设的色键，整个窗口就看不见了）。
    失败时恢复原样式并抛出 Win32Error。"""
    if not IS_WINDOWS:
        return False
    api = _api()
    old = api.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
    api.SetWindowLongW(hwnd, GWL_EXSTYLE, _signed32(old | PASSTHROUGH_BITS))
    if not has_passthrough_style(hwnd):
        error = api.get_last_error()
        api.SetWindowLongW(hwnd, GWL_EXSTYLE, _signed32(old))
        raise Win32Error("SetWindowLongW", error)
    return True


def is_window_valid(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return False
    api = _api()
    return bool(api.IsWindow(hwnd)) and bool(api.IsWindowVisible(hwnd))


def foreground_window() -> int | None:
    if not IS_WINDOWS:
        return None
    return _api().GetForegroundWindow() or None


def set_foreground(hwnd: int) -> bool:
    """把焦点还给别的窗口（比如面板启动时抢走了 PowerShell 的焦点）。"""
    if not IS_WINDOWS:
        return False
    return bool(_api().SetForegroundWindow(hwnd))


def is_minimized(hwnd: int) -> bool:
    if not IS_WINDOWS:
        return False
    return bool(_api().IsIconic(hwnd))


def client_rect(hwnd: int) -> Rect | None:
    """客户区（不含标题栏和边框）的屏幕坐标；最小化（0×0）时返回 None。"""
    if not IS_WINDOWS:
        return None
    api = _api()
    rect = api.wintypes.RECT()
    if not api.GetClientRect(hwnd, api.ctypes.byref(rect)):
        return None
    origin = api.wintypes.POINT(0, 0)
    if not api.ClientToScreen(hwnd, api.ctypes.byref(origin)):
        return None
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    return Rect(origin.x, origin.y, width, height)


def primary_button_down() -> bool | None:
    """鼠标主键（Tk 的 Button-1）现在是不是按着；非 Windows 返回 None（不知道）。
    GetAsyncKeyState 看的是物理按键，左右键对调时主键是物理右键。"""
    if not IS_WINDOWS:
        return None
    api = _api()
    vk = VK_RBUTTON if api.GetSystemMetrics(SM_SWAPBUTTON) else VK_LBUTTON
    return bool(api.GetAsyncKeyState(vk) & KEY_DOWN)


def virtual_screen() -> Rect | None:
    """所有显示器合起来的范围（主显示器左边或上边的显示器坐标为负）。"""
    if not IS_WINDOWS:
        return None
    metric = _api().GetSystemMetrics
    rect = Rect(
        metric(SM_XVIRTUALSCREEN),
        metric(SM_YVIRTUALSCREEN),
        metric(SM_CXVIRTUALSCREEN),
        metric(SM_CYVIRTUALSCREEN),
    )
    return rect if rect.width > 0 and rect.height > 0 else None


def _class_name(api: SimpleNamespace, hwnd: int) -> str:
    buffer = api.ctypes.create_unicode_buffer(256)
    if not api.GetClassNameW(hwnd, buffer, 256):
        return ""
    return buffer.value


def _process_path(api: SimpleNamespace, pid: int) -> str | None:
    handle = api.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = api.wintypes.DWORD(1024)
        buffer = api.ctypes.create_unicode_buffer(size.value)
        if not api.QueryFullProcessImageNameW(handle, 0, buffer, api.ctypes.byref(size)):
            return None
        return buffer.value
    finally:
        api.CloseHandle(handle)


def hearthstone_windows(require_unity: bool = True) -> list[tuple[int, str, int]]:
    """Hearthstone.exe 的可见顶层窗口：(句柄, 窗口类名, 客户区面积)。
    require_unity 时先按窗口类名筛掉别的窗口，不必打开它们的进程。"""
    if not IS_WINDOWS:
        return []
    api = _api()
    hwnds: list[int] = []

    def collect(hwnd, _lparam):
        if hwnd and api.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    callback = api.WNDENUMPROC(collect)  # 调用期间必须保持引用
    api.EnumWindows(callback, 0)
    paths: dict[int, str | None] = {}
    found: list[tuple[int, str, int]] = []
    for hwnd in hwnds:
        cls = _class_name(api, hwnd)
        if require_unity and cls != UNITY_CLASS:
            continue
        pid = api.wintypes.DWORD(0)
        api.GetWindowThreadProcessId(hwnd, api.ctypes.byref(pid))
        if not pid.value:
            continue
        if pid.value not in paths:
            paths[pid.value] = _process_path(api, pid.value)
        path = paths[pid.value]
        if path is None or not is_hearthstone_path(path):
            continue
        rect = client_rect(hwnd)
        found.append((hwnd, cls, rect.width * rect.height if rect is not None else 0))
    return found


def find_hearthstone() -> int | None:
    """炉石的主窗口：Unity 窗口中进程文件名是 Hearthstone.exe、客户区面积最大的那个。"""
    candidates = [(hwnd, area) for hwnd, _cls, area in hearthstone_windows() if area > 0]
    return pick_largest(candidates)
