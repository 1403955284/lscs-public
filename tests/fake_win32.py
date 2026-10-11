"""假的 Win32 API：在 Mac 上测试 lscs.overlay.win32 的逻辑（用法见 install）。

结构体（RECT、POINT、DWORD、字符串缓冲区）用真的 ctypes；函数按 Win32 的约定写回 byref 参数。
"""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

E_ACCESSDENIED = -2147024891  # 0x80070005，进程的 DPI 感知已经设置过


@dataclass
class FakeWindow:
    hwnd: int
    pid: int
    cls: str = "UnityWndClass"
    size: tuple[int, int] = (1920, 1080)
    origin: tuple[int, int] = (0, 0)
    visible: bool = True


class FakeWin32Api:
    def __init__(self, windows=(), paths=None, shcore=True, dpi_result=0, dpi_aware=1):
        self.ctypes = ctypes
        self.wintypes = wintypes
        self.WNDENUMPROC = lambda fn: fn
        self.windows = {w.hwnd: w for w in windows}
        self.paths = paths or {}  # pid → 进程路径；没有的 pid 打不开
        self.opened: list[int] = []
        self.closed: list[int] = []
        self.styles: dict[int, int] = {}
        self.alpha: dict[int, int] = {}
        self.layered_ok = True
        self.ancestor = 0xABC
        self.foreground = None
        self.focus_calls: list[int] = []
        self.dpi_calls: list[str] = []
        self._dpi_result = dpi_result
        self._dpi_aware = dpi_aware
        if not shcore:
            self.SetProcessDpiAwareness = None

    def install(self, monkeypatch):
        from lscs.overlay import win32

        monkeypatch.setattr(win32, "IS_WINDOWS", True)
        monkeypatch.setattr(win32, "_api_cache", self)
        return self

    def get_last_error(self):
        return 5

    # ---- 窗口 ----

    def EnumWindows(self, callback, lparam):
        for hwnd in self.windows:
            if not callback(hwnd, lparam):
                break
        return 1

    def IsWindow(self, hwnd):
        return int(hwnd in self.windows)

    def IsWindowVisible(self, hwnd):
        return int(hwnd in self.windows and self.windows[hwnd].visible)

    def IsIconic(self, hwnd):
        return 0

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self.windows[hwnd].cls
        return len(buffer.value)

    def GetWindowThreadProcessId(self, hwnd, pid_ref):
        pid_ref._obj.value = self.windows[hwnd].pid
        return 1

    def GetClientRect(self, hwnd, rect_ref):
        rect = rect_ref._obj
        rect.left = rect.top = 0
        rect.right, rect.bottom = self.windows[hwnd].size
        return 1

    def ClientToScreen(self, hwnd, point_ref):
        point_ref._obj.x, point_ref._obj.y = self.windows[hwnd].origin
        return 1

    def GetForegroundWindow(self):
        return self.foreground

    def SetForegroundWindow(self, hwnd):
        self.focus_calls.append(hwnd)
        self.foreground = hwnd
        return 1

    def GetAncestor(self, hwnd, flags):
        return self.ancestor

    def GetSystemMetrics(self, index):
        return {76: -1920, 77: 0, 78: 3840, 79: 1080}[index]

    # ---- 进程 ----

    def OpenProcess(self, access, inherit, pid):
        if pid not in self.paths:
            return None
        handle = 1000 + pid
        self.opened.append(handle)
        return handle

    def QueryFullProcessImageNameW(self, handle, flags, buffer, size_ref):
        path = self.paths[handle - 1000]
        if path is None:
            return 0  # 查询失败，句柄也要关闭
        buffer.value = path
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    # ---- 样式 ----

    def GetWindowLongW(self, hwnd, index):
        return self.styles.get(hwnd, 0)

    def SetWindowLongW(self, hwnd, index, value):
        previous = self.styles.get(hwnd, 0)
        self.styles[hwnd] = value & 0xFFFFFFFF
        return previous

    def SetLayeredWindowAttributes(self, hwnd, key, alpha, flags):
        if not self.layered_ok:
            return 0
        self.alpha[hwnd] = alpha
        return 1

    # ---- DPI ----

    def SetProcessDpiAwareness(self, value):
        self.dpi_calls.append("SetProcessDpiAwareness")
        return self._dpi_result

    def SetProcessDPIAware(self):
        self.dpi_calls.append("SetProcessDPIAware")
        return self._dpi_aware
