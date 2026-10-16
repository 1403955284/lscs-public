import signal
import threading
import time

from lscs.config import OverlayConfig
from lscs.overlay.app import Controller, run_with_overlay
from lscs.overlay.model import OverlayModel

CFG = OverlayConfig()


def model():
    return OverlayModel(lambda c: c)


class FakeWindow:
    status = "假的面板"

    def __init__(self, during=None):
        self.during = during  # 在"界面循环"里做的事，例如模拟 Ctrl+C
        self.polls = 0
        self.thread = None

    def run(self, _model, keep_running):
        self.thread = threading.current_thread()
        deadline = time.monotonic() + 5
        while keep_running():
            self.polls += 1
            if self.during is not None:
                self.during(self)
            assert time.monotonic() < deadline, "界面循环没有退出"
            time.sleep(0.01)


def until_stopped(log):
    def work(stop):
        log.append(("start", stop is not None, threading.current_thread().name))
        while stop is not None and not stop.is_set():
            stop.wait(0.01)
        log.append(("stopped",))

    return work


def test_controller_lingers_after_worker_ends():
    m, stop, now = model(), threading.Event(), [0.0]
    alive = [True]
    controller = Controller(m, stop, lambda: alive[0], "回放结束", clock=lambda: now[0], linger=3)
    assert controller.keep_running()
    alive[0] = False
    assert controller.keep_running()
    assert m.snapshot().title == "回放结束"
    now[0] = 2.9
    assert controller.keep_running()
    now[0] = 3.0
    assert not controller.keep_running()


def test_controller_stops_immediately_on_ctrl_c():
    stop = threading.Event()
    controller = Controller(model(), stop, lambda: True, "x")
    stop.set()
    assert not controller.keep_running()


def test_window_start_failure_falls_back_to_terminal():
    out, log = [], []

    def broken(_cfg):
        raise ImportError("No module named '_tkinter'")

    run_with_overlay(until_stopped(log), model(), CFG, create_window=broken, out=out.append)
    assert out == ["悬浮面板无法启动（Python 没有安装 tkinter），只在终端显示建议。"]
    # 在主线程里直接运行，没有停止标志（Ctrl+C 照常抛出 KeyboardInterrupt）
    assert log == [("start", False, threading.current_thread().name), ("stopped",)]


def test_other_start_failures_name_the_reason():
    out = []

    def broken(_cfg):
        raise RuntimeError("no display name and no $DISPLAY environment variable")

    run_with_overlay(lambda stop: None, model(), CFG, create_window=broken, out=out.append)
    assert "no display name" in out[0] and out[0].startswith("悬浮面板无法启动（")


def test_ctrl_c_stops_window_and_worker_and_restores_handler():
    out, log = [], []
    previous = signal.getsignal(signal.SIGINT)

    def press_ctrl_c(window):
        if window.polls == 3:
            signal.raise_signal(signal.SIGINT)  # 处理器只设置停止标志，不抛 KeyboardInterrupt

    window = FakeWindow(press_ctrl_c)
    run_with_overlay(
        until_stopped(log), model(), CFG, create_window=lambda _c: window, out=out.append
    )
    assert window.thread is threading.main_thread()  # 界面在主线程
    assert log[0][1:] == (True, "lscs-watch")  # 监听在后台线程
    assert log[-1] == ("stopped",)
    assert out == ["假的面板"]
    assert signal.getsignal(signal.SIGINT) is previous


def test_worker_ending_closes_window_with_message():
    m = model()
    shown = []

    def watch_title(window):
        shown.append(m.snapshot().title)

    run_with_overlay(
        lambda stop: None,
        m,
        CFG,
        create_window=lambda _c: FakeWindow(watch_title),
        finished_text="回放结束",
        linger=0.05,
        out=lambda _line: None,
    )
    assert shown[-1] == "回放结束"


def test_worker_crash_is_logged(caplog):
    def crash(_stop):
        raise RuntimeError("监听坏了")

    m = model()
    run_with_overlay(
        crash, m, CFG, create_window=lambda _c: FakeWindow(), linger=0.01, out=lambda _l: None
    )
    assert "监听坏了" in caplog.text
    assert m.snapshot().title == "监听已停止，请查看终端"


def test_window_error_keeps_watch_running_in_terminal():
    out, log = [], []
    finished = threading.Event()

    class BrokenWindow(FakeWindow):
        def run(self, _model, keep_running):
            raise RuntimeError("Tk 坏了")

    def work(stop):
        log.append("start")
        for _ in range(20):  # 面板坏了以后监听照常运行，直到自己结束
            if stop.wait(0.01):
                log.append("stopped early")
                return
        finished.set()

    run_with_overlay(work, model(), CFG, create_window=lambda _c: BrokenWindow(), out=out.append)
    assert finished.is_set() and "stopped early" not in log
    assert out[-1] == "悬浮面板出错已关闭，建议继续显示在终端。按 Ctrl+C 退出。"


def test_sigint_handler_restored_when_window_raises():
    previous = signal.getsignal(signal.SIGINT)

    class BrokenWindow(FakeWindow):
        def run(self, _model, keep_running):
            raise RuntimeError("Tk 坏了")

    run_with_overlay(
        lambda stop: None,
        model(),
        CFG,
        create_window=lambda _c: BrokenWindow(),
        out=lambda _l: None,
    )
    assert signal.getsignal(signal.SIGINT) is previous


def test_window_is_released_on_the_main_thread():
    """Tk 对象如果在后台线程被回收，Tcl 会报错甚至让进程崩溃；面板关闭后要在主线程释放。"""
    finalized = []

    class CyclicWindow(FakeWindow):
        def __init__(self):
            super().__init__()
            self.cycle = self  # 只有垃圾回收才能释放

        def __del__(self):
            finalized.append(threading.current_thread())

    def work(stop):
        stop.wait(0.05)

    def press_ctrl_c(window):
        if window.polls == 2:
            signal.raise_signal(signal.SIGINT)

    def factory(_cfg):
        window = CyclicWindow()
        window.during = press_ctrl_c
        return window

    run_with_overlay(work, model(), CFG, create_window=factory, out=lambda _l: None)
    assert finalized == [threading.main_thread()]


def test_fallback_runs_outside_the_exception_handler():
    import sys

    seen = []

    def broken(_cfg):
        raise ImportError("No module named '_tkinter'")

    def work(stop):
        seen.append(sys.exc_info()[0])

    run_with_overlay(work, model(), CFG, create_window=broken, out=lambda _l: None)
    assert seen == [None]  # 监听不是在 except 块里运行的（Ctrl+C 的回溯不会串上 ImportError）


def test_half_built_window_is_released_on_the_main_thread_before_fallback():
    """启动到一半失败的 Tk 对象要在主线程回收，不能留给后台线程（比如 lscs-llm）。"""
    events = []

    class HalfBuilt:
        def __init__(self):
            self.cycle = self

        def __del__(self):
            events.append(("released", threading.current_thread()))

    def broken(_cfg):
        half = HalfBuilt()  # noqa: F841 - 被异常的回溯引用着
        raise RuntimeError("no display")

    def work(_stop):
        events.append(("work", threading.current_thread()))

    run_with_overlay(work, model(), CFG, create_window=broken, out=lambda _l: None)
    main = threading.main_thread()
    assert events == [("released", main), ("work", main)]


def test_watch_window_remembers_its_position_in_the_project_file(monkeypatch):
    """lscs watch 的面板把拖到的位置记在 position.DEFAULT_PATH（运行时才取，所以测试里是
    conftest 换好的临时文件，不会写进项目文件夹）。"""
    import pytest

    pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay import app, position, window

    created = []

    class FakeOverlayWindow:
        def __init__(self, cfg, **kwargs):
            created.append((cfg, kwargs))

    monkeypatch.setattr(window, "OverlayWindow", FakeOverlayWindow)
    cfg, highlight = OverlayConfig(), HighlightConfig()
    app.create_tk_window(cfg, highlight=highlight)
    path = position.DEFAULT_PATH
    expected = {"highlight": highlight, "banner": False, "position_path": path, "art": None}
    assert created == [(cfg, expected)]
    assert position.DEFAULT_PATH.name == "overlay-position.json"
    app.create_tk_window(cfg, highlight=highlight, banner=True)  # [combat_result] 交给窗口
    assert created[-1][1]["banner"] is True


def test_watch_window_gets_an_art_cache_only_when_opponent_boards_are_on(monkeypatch, tmp_path):
    """[overlay] opponent_boards 开着、给了缓存目录时开卡图缓存交给窗口；关着时一张都不下载
    （不创建缓存）。窗口打不开时停掉缓存的线程。"""
    import pytest

    pytest.importorskip("tkinter")
    from lscs.cards import art as art_module
    from lscs.overlay import app, window

    caches, created = [], []

    class FakeArtCache:
        def __init__(self, cache_dir):
            self.cache_dir = cache_dir
            self.stopped = False
            caches.append(self)

        def stop(self):
            self.stopped = True

    class FakeOverlayWindow:
        def __init__(self, cfg, **kwargs):
            created.append(kwargs)
            if cfg.font_size == 33:
                raise RuntimeError("打不开")

    monkeypatch.setattr(art_module, "ArtCache", FakeArtCache)
    monkeypatch.setattr(window, "OverlayWindow", FakeOverlayWindow)
    app.create_tk_window(OverlayConfig(), art_dir=tmp_path / "art")
    assert len(caches) == 1 and caches[0].cache_dir == tmp_path / "art"
    assert created[-1]["art"] is caches[0] and not caches[0].stopped
    app.create_tk_window(OverlayConfig(opponent_boards=False), art_dir=tmp_path / "art")
    assert len(caches) == 1 and created[-1]["art"] is None
    with pytest.raises(RuntimeError):
        app.create_tk_window(OverlayConfig(font_size=33), art_dir=tmp_path / "art")
    assert caches[-1].stopped


def _banner(expires_at=None):
    from lscs.overlay.banner import Banner

    return Banner("本场已定：赢 · 洛卡拉掉 5 血", "win", expires_at)


def test_highlight_gets_the_step_and_the_banner_by_their_switches():
    import pytest

    pytest.importorskip("tkinter")
    from lscs.overlay.model import NextStep, OverlayView
    from lscs.overlay.window import highlight_inputs

    step = NextStep("▶ 刷新商店")
    view = OverlayView(title="t", next_step=step, banner=_banner())
    assert highlight_inputs(view, steps=True, banner=True, now=0) == (step, _banner(), ())
    # [highlight] enabled = false 但开着横幅：高亮窗口照样开，只是不画"下一步"
    assert highlight_inputs(view, steps=False, banner=True, now=0) == (None, _banner(), ())
    assert highlight_inputs(view, steps=True, banner=False, now=0) == (step, None, ())
    assert highlight_inputs(None, steps=True, banner=True, now=0) == (None, None, ())


def test_highlight_gets_the_core_targets_only_when_core_marks_are_on():
    import pytest

    pytest.importorskip("tkinter")
    from lscs.overlay.model import OverlayView, Target
    from lscs.overlay.window import highlight_inputs

    targets = (Target(row="shop", index=2, count=7),)
    view = OverlayView(title="t", core_targets=targets)
    assert highlight_inputs(view, steps=True, banner=True, now=0, core=True)[2] == targets
    assert highlight_inputs(view, steps=True, banner=True, now=0, core=False)[2] == ()
    assert highlight_inputs(view, steps=True, banner=True, now=0)[2] == ()


def test_banner_past_its_deadline_is_not_drawn():
    import pytest

    pytest.importorskip("tkinter")
    from lscs.overlay.model import OverlayView
    from lscs.overlay.window import highlight_inputs

    view = OverlayView(title="对局结束 · 第 3 名", banner=_banner(expires_at=120.0))
    assert highlight_inputs(view, steps=True, banner=True, now=119.5) == (None, view.banner, ())
    assert highlight_inputs(view, steps=True, banner=True, now=120.0) == (None, None, ())
