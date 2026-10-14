"""画面高亮的样式：四角括号、徽章的几何和"呼吸"状态机（纯函数，不开窗口）。"""

import pytest

from lscs.overlay.geometry import Rect
from lscs.overlay.layout import HighlightLayout, button_rect, content_rect, slot_rect
from lscs.overlay.style import (
    Breathing,
    Circle,
    Strokes,
    arm_length,
    badge_circle,
    badge_font_px,
    corner_brackets,
    dimmed,
    rounded_points,
    stroke_widths,
)

LAYOUT = HighlightLayout()
CLIENTS = {1080: Rect(0, 0, 1920, 1080), 1440: Rect(0, 0, 2560, 1440)}


@pytest.mark.parametrize(
    ("height", "expected"),
    [
        (1080, Strokes(under=5, line=2)),
        (1440, Strokes(under=7, line=3)),
        (360, Strokes(under=4, line=2)),  # 小窗口：衬底至少 4 像素、颜色线至少 2 像素
        (720, Strokes(under=4, line=2)),
    ],
)
def test_stroke_widths(height, expected):
    assert stroke_widths(height) == expected


@pytest.mark.parametrize(
    ("rect", "height", "arm"),
    [
        (Rect(0, 0, 119, 162), 1080, 26),  # 一张牌：短边的 22%
        (Rect(0, 0, 108, 108), 1080, 24),  # 按钮的外接正方形
        (Rect(0, 0, 158, 216), 1440, 35),
        (Rect(0, 0, 144, 144), 1440, 32),
        (Rect(0, 0, 356, 540), 1080, 32),  # 选项的大框：最长 0.03H
        (Rect(0, 0, 216, 324), 1440, 43),
        (Rect(0, 0, 40, 40), 1080, 13),  # 小框：最短 0.012H
        (Rect(0, 0, 20, 20), 1080, 10),  # 再小也不超过短边的一半，四个角不连成一圈
    ],
)
def test_arm_length_is_clamped(rect, height, arm):
    assert arm_length(rect, height) == arm


def test_corner_brackets_of_a_card_at_1080p():
    card = Rect(500, 315, 119, 162)
    # 每个角一个 L：(一条臂的端点, 角, 另一条臂的端点)；顺序左上、右上、右下、左下
    assert corner_brackets(card, 1080) == [
        ((500, 341), (500, 315), (526, 315)),
        ((593, 315), (619, 315), (619, 341)),
        ((619, 451), (619, 477), (593, 477)),
        ((526, 477), (500, 477), (500, 451)),
    ]


@pytest.mark.parametrize("height", [1080, 1440])
@pytest.mark.parametrize("kind", ["card", "button"])
def test_brackets_and_badge_of_real_targets(height, kind):
    client = CLIENTS[height]
    assert content_rect(client, LAYOUT.aspect).height == height
    if kind == "card":
        rect = slot_rect(LAYOUT, "shop", 0, 3, client)
    else:
        rect = button_rect(LAYOUT, "upgrade", client)
        assert rect.width == rect.height  # 按钮也画在外接正方形的四个角上
    arm = arm_length(rect, height)
    assert 0.012 * height - 0.5 <= arm <= 0.03 * height + 0.5  # 取整
    corners = [(rect.x, rect.y), (rect.right, rect.y), (rect.right, rect.bottom)]
    corners.append((rect.x, rect.bottom))
    brackets = corner_brackets(rect, height)
    assert [b[1] for b in brackets] == corners
    for start, corner, end in brackets:
        for tip in (start, end):  # 两条臂都沿着框的边、长度一样
            assert (tip[0] == corner[0]) != (tip[1] == corner[1])
            assert abs(tip[0] - corner[0]) + abs(tip[1] - corner[1]) == arm
    badge = badge_circle(rect, height)
    widths = stroke_widths(height)
    # 徽章在框右上角的正上方：圆心对着右边，整个圆在框外，和括号的深色衬底隔开一点
    assert badge.cx == rect.right
    assert badge.cy + badge.r < rect.y - widths.under / 2
    assert badge.cx + badge.r <= rect.right + 0.012 * height  # 往右只伸出半个徽章


@pytest.mark.parametrize(("height", "radius"), [(1080, 10), (1440, 14), (720, 9), (360, 9)])
def test_badge_radius_has_a_minimum(height, radius):
    assert badge_circle(Rect(100, 100, 60, 60), height).r == radius


def test_badge_circle_position():
    # 1080p：半径 10，衬底 5：圆心在右上角正上方 15 像素，圆的下沿离括号外沿 2.5 像素
    assert badge_circle(Rect(500, 315, 119, 162), 1080) == Circle(619, 300, 10)
    assert badge_circle(Rect(0, 100, 60, 60), 1440) == Circle(60, 79, 14)


def test_badge_font_fits_the_circle():
    assert badge_font_px(Circle(0, 0, 10)) == 12
    assert badge_font_px(Circle(0, 0, 14)) == 17
    assert badge_font_px(Circle(0, 0, 9)) == 11


def test_dimmed_blends_toward_the_dark_under_stroke():
    assert dimmed("#f2d98a") == "#978550"  # 45% 混向 #281e0a
    assert dimmed("#281e0a") == "#281e0a"
    assert dimmed("#FFFFFF", 0.0) == "#ffffff"
    assert dimmed("#000000", 1.0) == "#281e0a"


def test_banner_colors_match_the_panel():
    pytest.importorskip("tkinter")
    from lscs.overlay import window
    from lscs.overlay.style import BANNER_COLORS

    # 赢淡绿、输橙色、平浅灰：和面板的"复盘"标签、警告、标题同色
    assert BANNER_COLORS == {"win": window.REVIEW_TAG, "loss": window.WARN, "tie": window.TITLE}
    assert BANNER_COLORS == {"win": "#9fd3a0", "loss": "#ff9f43", "tie": "#d4d4dc"}


def test_rounded_points_clamp_the_radius():
    points = rounded_points(0, 0, 10, 40, 30)  # 半径超过短边的一半：按一半算
    assert len(points) == 24
    assert points[:2] == [5, 0]
    assert rounded_points(0, 0, 100, 100, 0)[:2] == [0, 0]


# ---- 呼吸 ----


class FakeTimers:
    """代替 Tk 的 after / after_cancel：手动一个一个触发。"""

    def __init__(self):
        self.pending = {}
        self.cancelled = []
        self.delays = []
        self._next = 0

    def after(self, ms, callback):
        self._next += 1
        self.pending[self._next] = callback
        self.delays.append(ms)
        return self._next

    def cancel(self, timer):
        self.cancelled.append(timer)
        del self.pending[timer]

    def fire(self, count=1):
        for _ in range(count):
            timer = min(self.pending)
            self.pending.pop(timer)()

    def fire_all(self, limit=100):
        while self.pending and limit:
            self.fire()
            limit -= 1


def breathing(**kwargs):
    timers, redraws = FakeTimers(), []
    pulse = Breathing(timers.after, timers.cancel, redraws.append, **kwargs)
    return pulse, timers, redraws


def test_a_new_target_pulses_three_times_then_stays_steady():
    pulse, timers, redraws = breathing()
    assert not pulse.dim and not pulse.active
    pulse.show(("商店1",))
    assert pulse.active and not pulse.dim and redraws == []  # 先按原色画，200 毫秒后第一次变暗
    timers.fire_all()
    assert redraws == [True, False, True, False, True, False]  # 6 次切换：暗 3 次，最后回到原色
    assert timers.delays == [200] * 6  # 共约 1.2 秒
    assert not pulse.active and not pulse.dim and timers.pending == {}


def test_the_same_target_does_not_pulse_again():
    pulse, timers, redraws = breathing()
    pulse.show(("商店1",))
    timers.fire(2)
    pulse.show(("商店1",))  # 呼吸中重画同一个目标：不从头开始
    assert len(timers.pending) == 1
    timers.fire_all()
    assert redraws == [True, False, True, False, True, False]
    pulse.show(("商店1",))  # 呼吸完再画同一个目标：不再呼吸
    assert timers.pending == {} and len(redraws) == 6 and not pulse.active


def test_a_change_mid_pulse_restarts():
    pulse, timers, redraws = breathing()
    pulse.show(("商店1",))
    timers.fire()
    assert pulse.dim
    first = next(iter(timers.pending))
    pulse.show(("刷新",))  # 换了目标：取消还没到的定时器，按原色从头开始
    assert timers.cancelled == [first]
    assert not pulse.dim and pulse.active and len(timers.pending) == 1
    redraws.clear()
    timers.fire_all()
    assert redraws == [True, False, True, False, True, False]


def test_hiding_cancels_the_pulse():
    pulse, timers, redraws = breathing()
    pulse.show(("商店1",))
    timers.fire()
    pulse.show(None)  # 面板隐藏、没有目标：取消定时器，回到原色，不再切换
    assert timers.pending == {} and len(timers.cancelled) == 1
    assert not pulse.dim and not pulse.active
    pulse.show(())
    assert timers.pending == {}
    pulse.show(("商店1",))  # 隐藏后再出现：重新呼吸一次
    assert pulse.active


def test_stop_cancels_and_is_idempotent():
    pulse, timers, _redraws = breathing()
    pulse.show(("商店1",))
    pulse.stop()  # 销毁窗口时
    pulse.stop()
    assert timers.pending == {} and len(timers.cancelled) == 1 and not pulse.active


def test_pulse_can_be_switched_off():
    pulse, timers, redraws = breathing(enabled=False)
    pulse.show(("商店1",))
    assert timers.pending == {} and redraws == [] and not pulse.active


def test_errors_in_the_callback_are_caught_and_end_the_pulse(caplog):
    timers = FakeTimers()
    calls = []

    def broken(dim):
        calls.append(dim)
        if dim:
            raise RuntimeError("画布没了")

    pulse = Breathing(timers.after, timers.cancel, broken)
    pulse.show(("商店1",))
    timers.fire()  # 异常不会跑出定时器回调
    assert timers.pending == {} and not pulse.active and not pulse.dim
    assert calls == [True, False]  # 出错后试着换回原色
    assert "高亮呼吸出错" in caplog.text
    pulse.show(("商店1",))  # 同一个目标：不再呼吸
    assert timers.pending == {}


def test_errors_while_scheduling_are_caught(caplog):
    timers = FakeTimers()
    fail = []

    def after(ms, callback):
        if fail:
            raise RuntimeError("after 坏了")
        return timers.after(ms, callback)

    pulse = Breathing(after, timers.cancel, lambda _dim: None)
    pulse.show(("商店1",))
    fail.append(True)
    timers.fire()
    assert not pulse.active and not pulse.dim
    assert "高亮呼吸出错" in caplog.text
    pulse.show(("刷新",))  # 开始时出错：也不抛出
    assert not pulse.active


def test_cancel_errors_are_ignored():
    timers = FakeTimers()

    def cancel(_timer):
        raise RuntimeError("窗口已经销毁")

    pulse = Breathing(timers.after, cancel, lambda _dim: None)
    pulse.show(("商店1",))
    pulse.stop()
    assert not pulse.active
