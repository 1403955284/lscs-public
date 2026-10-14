"""lscs highlight-test：把高亮能画的位置画出来（不真的开窗口，用假的窗口函数）。"""

import pytest
from fake_win32 import FakeWin32Api, FakeWindow

from lscs.config import HighlightConfig
from lscs.overlay import calibrate
from lscs.overlay.geometry import Rect
from lscs.overlay.layout import CALIBRATION_ROWS

HS = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"


class FakeShow:
    def __init__(self):
        self.calls = []
        self.banners = []

    def __call__(self, frames, client, seconds, preview, cfg, focus, banner):
        self.calls.append((frames, client, seconds, preview, cfg, focus))
        self.banners.append(banner)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def run(show, rows=CALIBRATION_ROWS, cfg=None, **kwargs):
    out = []
    ok = calibrate.run_highlight_test(
        cfg or HighlightConfig(), 3, rows, out=out.append, show=show, **kwargs
    )
    return ok, out


def test_windows_draws_every_slot_over_hearthstone(monkeypatch):
    api = FakeWin32Api(
        windows=[FakeWindow(1, pid=10, size=(2560, 1080), origin=(0, 0))], paths={10: HS}
    ).install(monkeypatch)
    show = FakeShow()
    ok, out = run(show)
    assert ok and api.dpi_calls  # 先设 DPI 感知，坐标才是物理像素
    frames, client, seconds, preview, _cfg, focus = show.calls[0]
    assert (client, seconds, preview) == (Rect(0, 0, 2560, 1080), 3, False)
    assert focus == 1  # 画好后把焦点还给炉石的窗口
    labels = [f.label for f in frames]
    assert labels[:7] == [f"商店{i}" for i in range(1, 8)]
    assert labels[-4:] == ["升本", "刷新", "冻结", "技能"]
    assert len(labels) == 7 + 7 + 3 + 4 + 4 + 4
    assert out[0] == "炉石客户区：2560×1080，左上角 (0, 0)"
    assert out[1] == "内容区（16:9）：1920×1080，左上角 (320, 0)"
    assert "  shop_y = 0.367  shop_pitch = 0.128  shop_width = 0.11  shop_height = 0.15" in out
    assert "  upgrade = [0.412, 0.175, 0.05]" in out
    assert out[-1] == calibrate.SCREENSHOT_HINT
    assert "默认只画招募阶段的商店、场面和已校准的按钮" in out[-1]
    assert "--rows choice" in out[-1] and "--rows hero" in out[-1]


def test_rows_can_be_chosen_and_config_values_are_used(monkeypatch):
    FakeWin32Api(windows=[FakeWindow(1, pid=10)], paths={10: HS}).install(monkeypatch)
    show = FakeShow()
    ok, out = run(show, rows=["choice4"], cfg=HighlightConfig(choice4_y=0.4))
    frames = show.calls[0][0]
    assert ok and [f.label for f in frames] == ["饰品1", "饰品2", "饰品3", "饰品4"]
    assert frames[0].rect.y + frames[0].rect.height / 2 == pytest.approx(0.4 * 1080, abs=1)
    assert any(line.startswith("  choice4_y = 0.4 ") for line in out)


def test_without_hearthstone_explains_and_draws_nothing(monkeypatch):
    FakeWin32Api().install(monkeypatch)
    show = FakeShow()
    ok, out = run(show)
    assert not ok and show.calls == []
    assert out == ["× 找炉石窗口：没找到炉石的窗口：请先打开炉石再运行"]


def test_minimized_hearthstone_is_waited_for(monkeypatch):
    api = FakeWin32Api(windows=[FakeWindow(1, pid=10, size=(0, 0))], paths={10: HS}).install(
        monkeypatch
    )
    clock, show = Clock(), FakeShow()

    def sleep(seconds):
        clock.sleep(seconds)
        if clock.now >= 2:
            api.windows[1].size = (1920, 1080)  # 玩家切回了炉石

    ok, out = run(show, clock=clock, sleep=sleep)
    assert ok and show.calls[0][1] == Rect(0, 0, 1920, 1080)
    assert out[0].startswith("炉石现在是最小化状态：请在 15 秒内切回炉石")
    clock, show = Clock(), FakeShow()
    api.windows[1].size = (0, 0)
    ok, out = run(show, clock=clock, sleep=clock.sleep)
    assert not ok and show.calls == []
    assert out[-1] == "× 等了 15 秒炉石还是最小化的：可以把炉石改成窗口模式后再运行一次。"


def test_other_platforms_draw_a_preview(monkeypatch):
    from lscs.overlay import win32

    monkeypatch.setattr(win32, "IS_WINDOWS", False)
    show = FakeShow()
    ok, out = run(show)
    _frames, client, _seconds, preview, _cfg, focus = show.calls[0]
    assert ok and preview and client == calibrate.PREVIEW_CLIENT and focus is None
    assert out[0].startswith("不是 Windows：在一个 1280×720 的预览窗口里画出要检查的位置")


def test_window_failure_is_reported(monkeypatch):
    from lscs.overlay import win32

    monkeypatch.setattr(win32, "IS_WINDOWS", False)

    def broken(*_args):
        raise RuntimeError("没有图形界面")

    ok, out = run(broken)
    assert not ok and out[-1] == "× 无法打开高亮窗口：没有图形界面"


def test_focus_goes_back_to_hearthstone(monkeypatch):
    api = FakeWin32Api(windows=[FakeWindow(1, pid=10)], paths={10: HS}).install(monkeypatch)
    api.foreground = 0x999  # 高亮窗口第一次显示时抢走了焦点
    calibrate.give_focus_to(1)
    assert api.focus_calls == [1]
    calibrate.give_focus_to(1)  # 炉石已经在前台：不再调用
    assert api.focus_calls == [1]

    def broken():
        raise OSError("GetForegroundWindow 坏了")

    api.GetForegroundWindow = broken
    calibrate.give_focus_to(1)  # 出错只记日志
    assert api.focus_calls == [1]


def test_show_tk_gives_focus_back_after_drawing(monkeypatch):
    """不开真的窗口：Tk 根和高亮窗口都换成假的，看画完之后才把焦点还给炉石。"""
    import tkinter

    api = FakeWin32Api(windows=[FakeWindow(1, pid=10)], paths={10: HS}).install(monkeypatch)
    order = []

    class FakeRoot:
        def withdraw(self):
            pass

        def after(self, _ms, callback):
            order.append("timer")

        def mainloop(self):
            order.append("mainloop")

        def quit(self):
            pass

        def destroy(self):
            order.append("destroy")

    class FakeHighlight:
        def __init__(self, _root, _cfg, _preview):
            api.foreground = 0x999  # 第一次显示时抢走焦点

        def draw(self, frames, client, banner=None):
            order.append(("draw", len(frames), client, banner))

        def destroy(self):
            pass

    set_foreground = api.SetForegroundWindow

    def focus(hwnd):
        order.append(("focus", hwnd))
        return set_foreground(hwnd)

    api.SetForegroundWindow = focus
    monkeypatch.setattr(tkinter, "Tk", FakeRoot)
    client = Rect(0, 0, 1920, 1080)
    banner = calibrate.SAMPLE_BANNER
    calibrate.show_tk([], client, 1, False, HighlightConfig(), 1, banner, make_window=FakeHighlight)
    assert order == [("draw", 0, client, banner), ("focus", 1), "timer", "mainloop", "destroy"]


def test_a_sample_banner_is_always_drawn(monkeypatch):
    """不管 --rows 选了什么，棋盘顶部都画一条样例横幅，截图时能一起核对它的位置。"""
    FakeWin32Api(windows=[FakeWindow(1, pid=10)], paths={10: HS}).install(monkeypatch)
    for rows in (CALIBRATION_ROWS, ["hero"]):
        show = FakeShow()
        ok, out = run(show, rows=rows)
        assert ok and show.banners == [calibrate.SAMPLE_BANNER]
        assert any("样例横幅" in line for line in out)
    banner = calibrate.SAMPLE_BANNER
    assert banner.text == "本场已定：赢 · 洛卡拉掉 5 血"
    assert (banner.tone, banner.expires_at) == ("win", None)
    assert "横幅" in calibrate.SCREENSHOT_HINT and "本场已定" in calibrate.SCREENSHOT_HINT
