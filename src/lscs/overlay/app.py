"""主线程跑 Tk 面板、后台线程跑日志监听；处理 Ctrl+C、面板启动失败和运行中出错。

- 面板无法启动（没有 tkinter、没有图形界面）：打印一行原因，在当前线程直接运行监听；
- Ctrl+C：SIGINT 处理器只设置停止标志（Tkinter 的回调包装会吞掉 KeyboardInterrupt），
  面板的定时器看到后关闭窗口，监听线程看到后退出；退出时恢复原来的处理器；
- 监听线程结束（回放完、或出错）：面板显示一行提示，几秒后关闭；
- 面板运行中出错：只记日志，监听继续在终端运行，直到它结束或 Ctrl+C。
"""

from __future__ import annotations

import gc
import logging
import signal
import threading
import time
from collections.abc import Callable
from typing import Protocol

from lscs.config import HighlightConfig, OverlayConfig
from lscs.overlay.model import OverlayModel

log = logging.getLogger(__name__)

FINISHED_TEXT = "监听已停止，请查看终端"
LINGER_SECONDS = 4.0  # 监听结束后面板再显示几秒
JOIN_SECONDS = 3.0

Work = Callable[[threading.Event | None], None]  # 参数为 None 时表示没有停止标志（只用终端）
Output = Callable[[str], None]


class OverlayWindowLike(Protocol):
    status: str  # 打开后在终端打印的一行说明

    def run(self, model: OverlayModel, keep_running: Callable[[], bool]) -> None:
        """在主线程运行界面，直到 keep_running() 返回 False。"""


WindowFactory = Callable[[OverlayConfig], OverlayWindowLike]


def _print(line: str) -> None:
    print(line, flush=True)


def create_tk_window(
    cfg: OverlayConfig, highlight: HighlightConfig | None = None, banner: bool = False
) -> OverlayWindowLike:
    """highlight：config.toml 的 [highlight]（画面高亮）；None 时不画。banner：在棋盘顶部画战斗
    结果的横幅（config.toml 的 [combat_result]）。拖到的位置记在项目文件夹的
    overlay-position.json（运行时才取 position.DEFAULT_PATH，测试里换成了临时目录）。"""
    from lscs.overlay import position
    from lscs.overlay.window import OverlayWindow  # 没有 tkinter 时在这里抛出 ImportError

    return OverlayWindow(
        cfg, highlight=highlight, banner=banner, position_path=position.DEFAULT_PATH
    )


class Controller:
    """界面每次轮询时调用 keep_running()。"""

    def __init__(
        self,
        model: OverlayModel,
        stop: threading.Event,
        worker_alive: Callable[[], bool],
        finished_text: str,
        clock: Callable[[], float] = time.monotonic,
        linger: float = LINGER_SECONDS,
    ) -> None:
        self._model = model
        self._stop = stop
        self._worker_alive = worker_alive
        self._finished_text = finished_text
        self._clock = clock
        self._linger = linger
        self._finished_at: float | None = None

    def keep_running(self) -> bool:
        if self._stop.is_set():
            return False
        if self._worker_alive():
            return True
        now = self._clock()
        if self._finished_at is None:
            self._finished_at = now
            self._model.show_message(self._finished_text)
        return now - self._finished_at < self._linger


def _reason(exc: Exception) -> str:
    if isinstance(exc, ImportError):
        return "Python 没有安装 tkinter"
    return str(exc) or type(exc).__name__


def _guarded(work: Work, stop: threading.Event) -> Callable[[], None]:
    def run() -> None:
        try:
            work(stop)
        except Exception:
            log.warning("监听出错，已停止", exc_info=True)

    return run


def _install_sigint(stop: threading.Event):
    """返回原来的处理器；不在主线程（不能装处理器）时返回 None。"""
    if threading.current_thread() is not threading.main_thread():
        return None
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
    return previous


def _restore_sigint(previous) -> None:
    if previous is not None:
        signal.signal(signal.SIGINT, previous)


def _wait_in_terminal(worker: threading.Thread) -> None:
    """面板关了但监听还在：像只用终端时一样等它，Ctrl+C 退出。"""
    try:
        while worker.is_alive():
            worker.join(0.2)
    except KeyboardInterrupt:
        pass


def run_with_overlay(
    work: Work,
    model: OverlayModel,
    cfg: OverlayConfig,
    *,
    create_window: WindowFactory = create_tk_window,
    finished_text: str = FINISHED_TEXT,
    linger: float = LINGER_SECONDS,
    out: Output = _print,
) -> None:
    window: OverlayWindowLike | None = None
    try:
        window = create_window(cfg)
    except Exception as exc:
        log.debug("悬浮面板无法启动", exc_info=True)
        out(f"悬浮面板无法启动（{_reason(exc)}），只在终端显示建议。")
    if window is None:
        # 启动到一半的 Tk 对象（被异常的回溯引用着）在主线程回收，不留给后台线程（比如 lscs-llm）
        gc.collect()
        work(None)  # 在 except 块外运行：Ctrl+C 的回溯不会串上启动失败的异常
        return
    out(window.status)
    stop = threading.Event()
    worker = threading.Thread(target=_guarded(work, stop), name="lscs-watch", daemon=True)
    controller = Controller(model, stop, worker.is_alive, finished_text, linger=linger)
    previous = _install_sigint(stop)
    try:
        worker.start()
        try:
            window.run(model, controller.keep_running)
        except Exception:
            log.warning("悬浮面板出错", exc_info=True)
        finally:
            # 在主线程释放 Tk 对象：它们如果在后台线程被回收，Tcl 会报错甚至让进程崩溃
            window = None
            gc.collect()
    finally:
        _restore_sigint(previous)
    if not stop.is_set() and worker.is_alive():
        out("悬浮面板出错已关闭，建议继续显示在终端。按 Ctrl+C 退出。")
        _wait_in_terminal(worker)
    stop.set()
    worker.join(JOIN_SECONDS)
