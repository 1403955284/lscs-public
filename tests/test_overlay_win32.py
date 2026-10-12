import sys

import pytest
from fake_win32 import E_ACCESSDENIED, FakeWin32Api, FakeWindow

from lscs.overlay import win32
from lscs.overlay.win32 import (
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TRANSPARENT,
    HearthstoneLocator,
    is_hearthstone_path,
    overlay_style,
    pick_largest,
    should_show,
)


@pytest.mark.skipif(sys.platform == "win32", reason="只检查非 Windows 上的空实现")
def test_non_windows_functions_do_nothing():
    assert win32.IS_WINDOWS is False
    assert not win32.set_dpi_awareness()
    assert win32.set_foreground(1) is False
    assert win32.hearthstone_windows() == []
    assert win32.root_window(123) is None
    assert win32.make_overlay(1, 0.85, True) is False
    assert win32.set_opacity(1, 0.0) is False
    assert win32.has_overlay_style(1, True) is False
    assert win32.find_hearthstone() is None
    assert win32.is_window_valid(1) is False
    assert win32.foreground_window() is None
    assert win32.is_minimized(1) is False
    assert win32.client_rect(1) is None
    assert win32.virtual_screen() is None


def test_hearthstone_path_matching():
    assert is_hearthstone_path(r"D:\Games\Hearthstone\Hearthstone.exe")
    assert is_hearthstone_path(r"C:\Program Files (x86)\Hearthstone\HEARTHSTONE.EXE")
    assert not is_hearthstone_path(r"C:\Program Files (x86)\Battle.net\Battle.net.exe")
    assert not is_hearthstone_path(r"C:\tools\Hearthstone Deck Tracker.exe")
    assert not is_hearthstone_path("")


def test_pick_largest_window():
    assert pick_largest([(1, 100), (2, 2_000_000), (3, 50)]) == 2
    assert pick_largest([]) is None


def test_overlay_style_bits():
    base = 0x100
    style = overlay_style(base, click_through=True)
    assert style == base | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    assert overlay_style(base, click_through=False) & WS_EX_TRANSPARENT == 0
    assert (WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE) == (
        0x80000,
        0x20,
        0x80,
        0x08000000,
    )


def test_should_show_only_when_hearthstone_or_panel_is_foreground():
    assert should_show(hearthstone=10, foreground=10, panel=20, minimized=False)
    assert should_show(hearthstone=10, foreground=20, panel=20, minimized=False)
    assert not should_show(hearthstone=10, foreground=30, panel=20, minimized=False)  # 切到别的程序
    assert not should_show(hearthstone=10, foreground=10, panel=20, minimized=True)
    assert not should_show(hearthstone=None, foreground=20, panel=20, minimized=False)
    assert not should_show(hearthstone=10, foreground=None, panel=None, minimized=False)
    # 画面高亮窗口在前台（比如刚创建时）也算
    assert should_show(hearthstone=10, foreground=40, panel=20, minimized=False, highlight=40)
    assert not should_show(hearthstone=10, foreground=40, panel=20, minimized=False, highlight=None)


class FakeWindows:
    def __init__(self):
        self.now = 0.0
        self.found = [None, 10, 11]
        self.valid = set()
        self.scans = 0

    def find(self):
        self.scans += 1
        hwnd = self.found.pop(0)
        if hwnd is not None:
            self.valid.add(hwnd)
        return hwnd


def test_locator_caches_and_rescans():
    fake = FakeWindows()
    locator = HearthstoneLocator(
        find=fake.find, valid=lambda h: h in fake.valid, clock=lambda: fake.now, rescan=2.0
    )
    assert locator.get() is None  # 第一次没找到
    fake.now = 1.0
    assert locator.get() is None  # 2 秒内不重找
    assert fake.scans == 1
    fake.now = 2.5
    assert locator.get() == 10
    fake.now = 3.0
    assert locator.get() == 10  # 句柄有效时直接用缓存
    assert fake.scans == 2
    fake.valid.discard(10)  # 炉石关闭或重启
    assert locator.get() == 11  # 句柄失效立即重找
    assert fake.scans == 3


HS = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"


def test_find_hearthstone_filters_by_class_and_exe_and_takes_largest(monkeypatch):
    api = FakeWin32Api(
        windows=[
            FakeWindow(1, pid=10, size=(800, 600)),  # 炉石的小窗口（比如启动画面）
            FakeWindow(2, pid=10, size=(1920, 1080), origin=(0, 0)),
            FakeWindow(3, pid=20, size=(2560, 1440)),  # 别的 Unity 游戏
            FakeWindow(4, pid=30, cls="ConsoleWindowClass"),  # 不是 Unity 窗口，不打开进程
            FakeWindow(5, pid=10, size=(3840, 2160), visible=False),  # 不可见
        ],
        paths={10: HS, 20: r"D:\Games\Other\Other.exe", 30: HS},
    ).install(monkeypatch)
    assert win32.find_hearthstone() == 2
    assert sorted(api.opened) == [1010, 1020]  # 每个进程只打开一次，控制台窗口没打开
    assert sorted(api.closed) == sorted(api.opened)  # 句柄全部关闭
    assert win32.hearthstone_windows(require_unity=False) == [
        (1, "UnityWndClass", 480000),
        (2, "UnityWndClass", 2073600),
        (4, "ConsoleWindowClass", 2073600),
    ]


def test_process_handle_is_closed_even_when_query_fails(monkeypatch):
    api = FakeWin32Api(windows=[FakeWindow(1, pid=10)], paths={10: None}).install(monkeypatch)
    assert win32.find_hearthstone() is None
    assert api.opened == [1010] and api.closed == [1010]


def test_set_dpi_awareness_falls_back(monkeypatch):
    api = FakeWin32Api().install(monkeypatch)
    assert win32.set_dpi_awareness() == "SetProcessDpiAwareness"
    api = FakeWin32Api(dpi_result=E_ACCESSDENIED).install(monkeypatch)
    assert win32.set_dpi_awareness() == "SetProcessDPIAware"
    assert api.dpi_calls == ["SetProcessDpiAwareness", "SetProcessDPIAware"]
    FakeWin32Api(shcore=False).install(monkeypatch)  # Windows 8.1 以前没有 shcore
    assert win32.set_dpi_awareness() == "SetProcessDPIAware"
    FakeWin32Api(dpi_result=E_ACCESSDENIED, dpi_aware=0).install(monkeypatch)
    assert win32.set_dpi_awareness() == ""


def test_client_rect(monkeypatch):
    FakeWin32Api(
        windows=[
            FakeWindow(1, pid=10, size=(1600, 900), origin=(8, 31)),
            FakeWindow(2, 10, size=(0, 0)),
        ]
    ).install(monkeypatch)
    assert win32.client_rect(1) == win32.Rect(8, 31, 1600, 900)
    assert win32.client_rect(2) is None  # 最小化时客户区是 0×0


def test_virtual_screen_and_foreground(monkeypatch):
    api = FakeWin32Api().install(monkeypatch)
    assert win32.virtual_screen() == win32.Rect(-1920, 0, 3840, 1080)
    assert win32.foreground_window() is None
    assert win32.set_foreground(7) is True and api.foreground == 7


def test_make_overlay_sets_style_and_alpha(monkeypatch):
    api = FakeWin32Api().install(monkeypatch)
    api.styles[9] = 0x100
    assert win32.make_overlay(9, 0.85, click_through=True) is True
    assert win32.has_overlay_style(9, click_through=True)
    assert api.alpha[9] == 217
    assert win32.set_opacity(9, 0.0) is True and api.alpha[9] == 0


def test_make_overlay_failure_names_the_step_and_restores_style(monkeypatch):
    api = FakeWin32Api().install(monkeypatch)
    api.styles[9] = 0x100
    api.layered_ok = False
    with pytest.raises(win32.Win32Error, match="SetLayeredWindowAttributes 失败（错误码 5）"):
        win32.make_overlay(9, 0.85, click_through=True)
    assert api.styles[9] == 0x100


def test_make_overlay_reports_style_that_did_not_stick(monkeypatch):
    api = FakeWin32Api().install(monkeypatch)
    api.SetWindowLongW = lambda hwnd, index, value: 0  # 什么都没改
    with pytest.raises(win32.Win32Error, match="SetWindowLongW"):
        win32.make_overlay(9, 0.85, click_through=True)


@pytest.mark.skipif(sys.platform != "win32", reason="真的调用 Win32（只在 Windows 上）")
def test_real_win32_helpers():
    win32.load_api()
    assert isinstance(win32.set_dpi_awareness(), str)
    screen = win32.virtual_screen()
    assert screen is not None and screen.width > 0 and screen.height > 0
    foreground = win32.foreground_window()
    assert foreground is None or isinstance(foreground, int)
    hearthstone = win32.find_hearthstone()
    assert hearthstone is None or win32.is_window_valid(hearthstone)
    assert win32.is_window_valid(0) is False
    assert isinstance(win32.hearthstone_windows(require_unity=False), list)


# ---- 画面高亮的窗口 ----


def test_make_passthrough_only_changes_style_bits(monkeypatch):
    api = FakeWin32Api().install(monkeypatch)
    api.styles[9] = WS_EX_LAYERED | 0x100  # Tk 设色键时已经是分层窗口
    assert win32.has_passthrough_style(9) is False
    assert win32.make_passthrough(9) is True
    assert (
        api.styles[9]
        == WS_EX_LAYERED | 0x100 | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    )
    assert win32.has_passthrough_style(9) is True
    assert api.alpha == {}  # 不调 SetLayeredWindowAttributes：LWA_ALPHA 会盖掉色键


def test_make_passthrough_reports_style_that_did_not_stick(monkeypatch):
    api = FakeWin32Api().install(monkeypatch)
    api.styles[9] = 0x100
    calls = []

    def set_style(hwnd, index, value):
        calls.append(value)
        return 0  # 什么都没改

    api.SetWindowLongW = set_style
    with pytest.raises(win32.Win32Error, match="SetWindowLongW"):
        win32.make_passthrough(9)
    assert calls[-1] == 0x100  # 恢复原样


@pytest.mark.skipif(sys.platform == "win32", reason="只检查非 Windows 上的空实现")
def test_passthrough_does_nothing_off_windows():
    assert win32.make_passthrough(1) is False
    assert win32.has_passthrough_style(1) is False
