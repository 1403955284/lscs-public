"""lscs highlight-test：把画面高亮能画的位置画出来，框里写标签，供用户截图核对、开发者校准
（选项建议设计文档第 9 节）。默认只画招募阶段的商店、场面每个槽位和已校准的按钮；--rows 可以
选别的排（选项、饰品、英雄），免得几排框叠在一起看不清。

- Windows：找炉石窗口（和 overlay-check 同样的查找和提示），在炉石客户区上盖一个透明、
  鼠标穿透的高亮窗口，画好后把焦点还给炉石（高亮窗口第一次显示时可能抢走焦点，全屏的炉石
  一失去焦点就会最小化）；全屏的炉石在运行命令时会被最小化，所以最多等 15 秒让玩家切回去；
- 其他平台：开一个 1280×720 的深色预览窗口，画同样的框（开发时检查几何用）；
- 不读建议、不碰 AI，只用 config.toml 的 [highlight]（没有文件时用默认值）。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence

from lscs.config import HighlightConfig
from lscs.overlay import check, win32
from lscs.overlay.geometry import Rect
from lscs.overlay.layout import (
    Frame,
    HighlightLayout,
    calibration_frames,
    content_rect,
    parameter_lines,
)

log = logging.getLogger(__name__)

Output = Callable[[str], None]
# (框, 客户区, 秒数, 是否预览, [highlight], 画好后把焦点还给哪个窗口)
Show = Callable[[list[Frame], Rect, float, bool, HighlightConfig, int | None], None]
PREVIEW_CLIENT = Rect(60, 60, 1280, 720)  # 非 Windows 的预览窗口
WAIT_SECONDS = 15.0  # 炉石最小化时最多等多久
POLL_SECONDS = 0.5
SCREENSHOT_HINT = (
    "默认只画招募阶段的商店、场面和已校准的按钮。框对不准时截一张图发给开发者（运行时切到炉石截图，"
    "框到时间自动消失；需要更久可以加 --seconds 60）。弹出选项时可以用 --rows choice（4 个选项用 "
    "choice4），选英雄时用 --rows hero 单独检查。"
)


def _print(line: str) -> None:
    print(line, flush=True)


def _reason(exc: BaseException) -> str:
    return str(exc) or type(exc).__name__


def give_focus_to(hwnd: int) -> None:
    """把焦点还给炉石（已经在前台时什么都不做）。失败只记日志：框照样画着。"""
    try:
        if win32.foreground_window() != hwnd:
            win32.set_foreground(hwnd)
    except Exception:
        log.debug("把焦点还给炉石时出错", exc_info=True)


def _highlight_window(root, cfg: HighlightConfig, preview: bool):
    from lscs.overlay.highlight import HighlightWindow

    return HighlightWindow(root, cfg, preview=preview)


def show_tk(
    frames: list[Frame],
    client: Rect,
    seconds: float,
    preview: bool,
    cfg: HighlightConfig,
    focus: int | None = None,
    make_window: Callable = _highlight_window,
) -> None:
    """开一个隐藏的 Tk 根窗口和高亮窗口，画上 frames，seconds 秒后关闭。
    focus：画好（穿透样式也加上了）之后把焦点还给这个窗口（炉石）。"""
    import tkinter as tk

    root = tk.Tk()
    try:
        root.withdraw()
        window = make_window(root, cfg, preview)
        window.draw(frames, client)
        if focus is not None:
            give_focus_to(focus)
        root.after(max(1, round(seconds * 1000)), root.quit)
        root.mainloop()
        window.destroy()
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass


def _hearthstone_client(
    out: Output, clock: Callable[[], float], sleep: Callable[[float], None]
) -> tuple[int, Rect] | None:
    """炉石的窗口句柄和客户区；最小化时等玩家切回去，最多 WAIT_SECONDS 秒。
    找不到时说明原因、返回 None。"""
    deadline: float | None = None
    while True:
        try:
            hwnd, rect = check.locate_hearthstone()
        except check.Note:
            hwnd, rect = None, None
        except Exception as exc:
            log.debug("highlight-test：找炉石窗口失败", exc_info=True)
            out(f"× 找炉石窗口：{_reason(exc)}")
            return None
        if hwnd is not None and rect is not None:
            return hwnd, rect
        if deadline is None:
            out(
                f"炉石现在是最小化状态：请在 {WAIT_SECONDS:g} 秒内切回炉石"
                "（全屏的炉石在运行命令时会自动最小化）。"
            )
            deadline = clock() + WAIT_SECONDS
        elif clock() >= deadline:
            out(f"× 等了 {WAIT_SECONDS:g} 秒炉石还是最小化的：可以把炉石改成窗口模式后再运行一次。")
            return None
        sleep(POLL_SECONDS)


def run_highlight_test(
    cfg: HighlightConfig,
    seconds: float,
    rows: Sequence[str],
    out: Output = _print,
    show: Show | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """画好并显示 seconds 秒后返回 True；找不到炉石或窗口打不开时说明原因、返回 False。
    show 默认是 show_tk（真的开窗口）。"""
    show = show or show_tk
    layout = HighlightLayout.from_config(cfg)
    preview = not win32.IS_WINDOWS
    hearthstone: int | None = None  # 画好后把焦点还给它
    if preview:
        out(
            f"不是 Windows：在一个 {PREVIEW_CLIENT.width}×{PREVIEW_CLIENT.height} 的预览窗口里"
            "画出要检查的位置（只用来检查几何，不贴炉石）。"
        )
        client = PREVIEW_CLIENT
    else:
        try:
            win32.set_dpi_awareness()  # 必须在创建窗口之前：坐标才是物理像素
        except Exception:
            log.warning("设置 DPI 感知失败，位置可能有偏差", exc_info=True)
        found = _hearthstone_client(out, clock, sleep)
        if found is None:
            return False
        hearthstone, client = found
    content = content_rect(client, layout.aspect)
    where = "预览窗口" if preview else "炉石客户区"
    out(f"{where}：{client.width}×{client.height}，左上角 ({client.x}, {client.y})")
    ratio = "16:9" if abs(layout.aspect - 16 / 9) < 1e-6 else f"{layout.aspect:.4f}"
    out(f"内容区（{ratio}）：{content.width}×{content.height}，左上角 ({content.x}, {content.y})")
    out("用的参数（config.toml 的 [highlight] 可以覆盖）：")
    for line in parameter_lines(layout):
        out(f"  {line}")
    frames = calibration_frames(layout, client, rows)
    out(f"画了 {len(frames)} 个框，{seconds:g} 秒后自动关闭。")
    if not preview:
        out(SCREENSHOT_HINT)
    try:
        show(frames, client, seconds, preview, cfg, hearthstone)
    except Exception as exc:
        log.debug("highlight-test：窗口出错", exc_info=True)
        out(f"× 无法打开高亮窗口：{_reason(exc)}")
        return False
    return True
