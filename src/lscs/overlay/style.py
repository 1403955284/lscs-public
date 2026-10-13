"""画面高亮的样式（纯函数和纯状态机，不碰 Tk，任何平台都可以测试）：四角括号、细线框、徽章的几何，
以及新目标出现时短暂的"呼吸"。highlight.py 只负责把这里算好的东西画到画布上。

尺寸按内容区高度 H 的比例算，再取一个最小像素数，小窗口里也看得清。色键窗口不能半透明，所以
没有发光：每一笔先画一条宽一点的深色衬底，再在上面画颜色线，亮的卡图上也看得清。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Any

from lscs.overlay.geometry import Rect

log = logging.getLogger(__name__)

DARK = "#281e0a"  # 深色衬底
BADGE_FILL = "#1e1e26"  # 徽章的底色
BADGE_RING = 2  # 徽章外圈（高亮颜色）的宽度
ARM, ARM_MIN, ARM_MAX = 0.22, 0.012, 0.03  # 括号的臂长：短边的 22%，限制在 0.012H~0.03H
UNDER_WIDTH, UNDER_MIN = 0.005, 4  # 深色衬底的宽度（H 的比例、最少几像素）
LINE_WIDTH, LINE_MIN = 0.0022, 2  # 颜色线的宽度
BADGE_RADIUS, BADGE_MIN = 0.0095, 9  # 徽章半径
BADGE_FONT = 1.2  # 徽章字号 = 半径 × 1.2
CORNER = 0.015  # 细线框（style = "frame"）和校准框的圆角半径
DIM = 0.45  # 呼吸时的暗色：高亮颜色往 DARK 混 45%
PULSE_MS = 200  # 呼吸时每隔多久切换一次
PULSE_TOGGLES = 6  # 切换几次：暗 3 次，最后回到原色，共约 1.2 秒

Point = tuple[float, float]
Polyline = tuple[Point, ...]


@dataclass(frozen=True)
class Strokes:
    under: int  # 深色衬底的宽度
    line: int  # 颜色线的宽度


@dataclass(frozen=True)
class Circle:
    cx: float
    cy: float
    r: float


def stroke_widths(height: int) -> Strokes:
    return Strokes(
        under=max(UNDER_MIN, round(UNDER_WIDTH * height)),
        line=max(LINE_MIN, round(LINE_WIDTH * height)),
    )


def arm_length(rect: Rect, height: int) -> int:
    """括号一条臂的长度：短边的 22%，限制在 [0.012H, 0.03H]，也不超过短边的一半。"""
    short = min(rect.width, rect.height)
    arm = min(max(ARM * short, ARM_MIN * height), ARM_MAX * height, short / 2)
    return round(arm)


def corner_brackets(rect: Rect, height: int) -> list[Polyline]:
    """四个角各一个 L：(一条臂的端点, 角, 另一条臂的端点)，顺序左上、右上、右下、左下。
    按钮（圆）也画在它的外接正方形上。"""
    a = arm_length(rect, height)
    x0, y0, x1, y1 = rect.x, rect.y, rect.right, rect.bottom
    return [
        ((x0, y0 + a), (x0, y0), (x0 + a, y0)),
        ((x1 - a, y0), (x1, y0), (x1, y0 + a)),
        ((x1, y1 - a), (x1, y1), (x1 - a, y1)),
        ((x0 + a, y1), (x0, y1), (x0, y1 - a)),
    ]


def badge_circle(rect: Rect, height: int) -> Circle:
    """步骤编号的小圆：在框右上角的正上方（圆心对着框的右边），整个圆在框外，下沿和括号的
    深色衬底隔开半个衬底宽。往右只伸出半个圆，不会压到右边挨着的按钮（刷新旁边就是冻结）。"""
    r = max(BADGE_MIN, round(BADGE_RADIUS * height))
    return Circle(rect.right, rect.y - r - stroke_widths(height).under, r)


def badge_font_px(circle: Circle) -> int:
    return round(BADGE_FONT * circle.r)


def rounded_points(x0: float, y0: float, x1: float, y1: float, r: float) -> list[float]:
    """圆角矩形的多边形顶点（配合 Tk 的 smooth=True）；半径不超过短边的一半。"""
    r = max(0.0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    return [
        *(x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r),
        *(x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1),
        *(x0, y1, x0, y1 - r, x0, y0 + r, x0, y0),
    ]


def dimmed(color: str, amount: float = DIM, toward: str = DARK) -> str:
    """color 往 toward 混 amount（0~1）：呼吸时的暗色。"""

    def rgb(value: str) -> tuple[int, ...]:
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))

    mixed = (round(a + (b - a) * amount) for a, b in zip(rgb(color), rgb(toward), strict=True))
    return "#" + "".join(f"{c:02x}" for c in mixed)


class Breathing:
    """新目标出现时短暂"呼吸"：每 interval_ms 毫秒在原色和暗色之间切换一次，共 toggles 次
    （暗 3 次，最后回到原色），之后一直是原色。同一个目标重画时不再呼吸；隐藏时取消。

    纯状态机：定时器用调用方给的 after(ms, callback) -> id 和 cancel(id)（Tk 的 after /
    after_cancel，只在主线程用）；redraw(dim) 把线条换成暗色或原色。新目标的框由调用方按原色
    画好，这里只管之后的切换。回调里的异常只记日志，呼吸就此停在原色。
    """

    def __init__(
        self,
        after: Callable[[int, Callable[[], None]], Any],
        cancel: Callable[[Any], None],
        redraw: Callable[[bool], None],
        enabled: bool = True,
        toggles: int = PULSE_TOGGLES,
        interval_ms: int = PULSE_MS,
    ) -> None:
        self._after = after
        self._cancel = cancel
        self._redraw = redraw
        self._enabled = enabled
        self._toggles = toggles
        self._interval = interval_ms
        self._key: Hashable | None = None
        self._left = 0
        self._dim = False
        self._timer: Any = None

    @property
    def dim(self) -> bool:
        """现在该用暗色画线。"""
        return self._dim

    @property
    def active(self) -> bool:
        """还有没到的定时器。"""
        return self._timer is not None

    def show(self, key: Hashable | None) -> None:
        """key 是现在画的目标（None 或空：隐藏了、没有目标）。目标变了：取消正在进行的呼吸，
        从原色重新开始；同一个目标：什么都不做。"""
        if not key:
            self.stop()
            return
        if key == self._key:
            return
        self.stop()
        self._key = key
        if not self._enabled:
            return
        self._left = self._toggles
        try:
            self._timer = self._after(self._interval, self._tick)
        except Exception:
            self._fail()

    def stop(self) -> None:
        """隐藏、没有目标、销毁窗口时：取消还没到的定时器，回到原色（不重画：调用方会清空或重画）。"""
        timer, self._timer = self._timer, None
        self._key = None
        self._left = 0
        self._dim = False
        if timer is not None:
            try:
                self._cancel(timer)
            except Exception:
                log.debug("取消高亮呼吸的定时器时出错", exc_info=True)

    def _tick(self) -> None:
        self._timer = None
        try:
            self._dim = not self._dim
            self._left -= 1
            self._redraw(self._dim)
            if self._left > 0:
                self._timer = self._after(self._interval, self._tick)
        except Exception:
            self._fail()

    def _fail(self) -> None:
        """出错：记日志，停在原色（目标不变，不会再呼吸）。"""
        log.warning("高亮呼吸出错", exc_info=True)
        self._timer = None
        self._left = 0
        if self._dim:
            self._dim = False
            try:
                self._redraw(False)
            except Exception:
                log.debug("高亮呼吸出错后换回原色时又出错", exc_info=True)
