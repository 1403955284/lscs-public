"""纯函数：按炉石客户区和屏幕大小计算面板的位置、宽度、字号，以及文字换行。

位置用锚点（Anchor）表示：面板（连同上面的拖动标签，合起来叫"箱子"）的哪条边贴在参照区域的
哪个比例上。预设和拖动后记住的位置都是锚点，换分辨率、拉窗口时按比例跟着走。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

FONT_DIVISOR = 55  # 自动字号约为客户区高度的 1/55
MIN_FONT, MAX_FONT = 12, 22
MAX_FONT_DIVISOR = 45  # 上限随高度变大：max(22, 高度 / 45)，4K 下字不会太小
CHARS_PER_LINE = 22  # 面板宽度按字号算：每行约 22 个汉字
TAG_CHARS = 2.5  # 左边"规则"/"AI"标签列约 2.5 个字宽
MAX_WIDTH_RATIO = 0.35  # 但不超过客户区宽度的 35%
MIN_WIDTH = 160
MAX_HEIGHT_RATIO = 0.45  # 面板最高为客户区高度的 45%，超出时界面逐级精简内容
PAD_RATIO = 0.6  # 内边距约 0.6 个字高
SIDE_MARGIN = 0.02  # 左右位置：距客户区边缘 2% 宽度
BOTTOM_MARGIN = 0.02  # 左右位置：距客户区底边 2% 高度，向上长（避开"开始战斗"按钮）
TOP_MARGIN = 0.02  # 顶部位置：距客户区顶部 2% 高度
CORNER_TOP = 0.05  # 没有炉石窗口可贴时放在屏幕右上角，距顶部 5% 高度（避开 Mac 菜单栏）
GRIP_CHARS, GRIP_HEIGHT = 4.5, 1.4  # 拖动标签的宽、高（字号的倍数）
DRAG_THRESHOLD = 3  # 按住标签移动这么多像素才算拖（点一下不写文件）

STEP_SEP = "  "  # 方案各步之间的分隔
Position = str  # "right" | "left" | "top"
Measure = Callable[[str], int]


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height


@dataclass(frozen=True)
class Anchor:
    x: float  # 锚定边在参照区域宽度上的比例，0~1
    y: float  # 锚定边在参照区域高度上的比例，0~1
    h: Literal["left", "center", "right"]  # x 指的是箱子的左边、中线还是右边
    v: Literal["top", "bottom"]  # y 指的是箱子的上边还是下边


PRESETS: dict[str, Anchor] = {
    "right": Anchor(1 - SIDE_MARGIN, 1 - BOTTOM_MARGIN, "right", "bottom"),  # 右下角
    "left": Anchor(SIDE_MARGIN, 1 - BOTTOM_MARGIN, "left", "bottom"),  # 左下角
    "top": Anchor(0.5, TOP_MARGIN, "center", "top"),  # 顶部居中
}
CORNER = Anchor(1 - SIDE_MARGIN, CORNER_TOP, "right", "top")  # 贴不上炉石时：屏幕右上角


@dataclass(frozen=True)
class Layout:
    width: int  # 面板宽度（像素）
    font_px: int  # 字号（像素）
    pad: int  # 内边距（像素）
    max_height: int  # 面板最大高度（像素）


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def font_px(client_height: int, font_size: int = 0) -> int:
    """font_size 为 0 时按客户区高度自动计算，否则直接使用（配置里已限制 10~40）。"""
    if font_size:
        return font_size
    upper = max(MAX_FONT, round(client_height / MAX_FONT_DIVISOR))
    return _clamp(round(client_height / FONT_DIVISOR), MIN_FONT, upper)


def panel_layout(client: Rect, font_size: int = 0) -> Layout:
    """宽度按字号算（手动调大字号时面板也跟着变宽），再限制在客户区宽度的 35% 以内。"""
    px = font_px(client.height, font_size)
    pad = round(px * PAD_RATIO)
    natural = round(px * (CHARS_PER_LINE + TAG_CHARS)) + 2 * pad
    width = max(MIN_WIDTH, min(natural, round(client.width * MAX_WIDTH_RATIO)))
    return Layout(
        width=width, font_px=px, pad=pad, max_height=round(client.height * MAX_HEIGHT_RATIO)
    )


def _keep_in(x: int, y: int, width: int, height: int, rect: Rect) -> tuple[int, int]:
    # 箱子比区域还大时贴区域左上角
    x = max(rect.x, min(x, rect.right - width))
    y = max(rect.y, min(y, rect.bottom - height))
    return x, y


def keep_inside(
    x: int, y: int, width: int, height: int, area: Rect, screen: Rect
) -> tuple[int, int]:
    """把箱子先夹进参照区域（炉石客户区），再夹到屏幕里。"""
    x, y = _keep_in(x, y, width, height, area)
    return _keep_in(x, y, width, height, screen)


def dock(area: Rect, screen: Rect, width: int, height: int, anchor: Anchor) -> tuple[int, int]:
    """箱子（面板 + 拖动标签）左上角的屏幕坐标：锚定边放在区域的 anchor 比例上，
    再夹进区域和屏幕。区域是炉石客户区，贴不上炉石时是屏幕本身。"""
    x = area.x + round(area.width * anchor.x)
    if anchor.h == "right":
        x -= width
    elif anchor.h == "center":
        x -= width // 2
    y = area.y + round(area.height * anchor.y)
    if anchor.v == "bottom":
        y -= height
    return keep_inside(x, y, width, height, area, screen)


def _ratio(offset: int, size: int) -> float:
    return min(1.0, max(0.0, round(offset / size, 4))) if size > 0 else 0.0


def anchor_from_rect(box: Rect, area: Rect) -> Anchor:
    """拖到的位置换成锚点：记住离得近的那两条边（中心在左半边记左边，否则记右边；在上半边记
    上边，否则记下边），所以放在下半边的面板向上长，换分辨率时贴着的那条边不变。"""
    if 2 * box.x + box.width < 2 * area.x + area.width:
        h, x = "left", _ratio(box.x - area.x, area.width)
    else:
        h, x = "right", _ratio(box.right - area.x, area.width)
    if 2 * box.y + box.height < 2 * area.y + area.height:
        v, y = "top", _ratio(box.y - area.y, area.height)
    else:
        v, y = "bottom", _ratio(box.bottom - area.y, area.height)
    return Anchor(x, y, h, v)


def grip_size(layout: Layout) -> tuple[int, int]:
    """拖动标签的宽、高：约 4.5 个字宽、1.4 个字高。"""
    return round(layout.font_px * GRIP_CHARS), round(layout.font_px * GRIP_HEIGHT)


def split_box(
    x: int, y: int, width: int, panel_height: int, grip_w: int, grip_h: int
) -> tuple[Rect, Rect]:
    """(面板, 拖动标签)：标签贴在箱子右上角，面板在标签正下方，两者不重叠。"""
    panel = Rect(x, y + grip_h, width, panel_height)
    grip = Rect(x + width - grip_w, y, grip_w, grip_h)
    return panel, grip


@dataclass(frozen=True)
class Drag:
    """按下拖动标签时鼠标和箱子左上角的位置（屏幕坐标）。"""

    mouse_x: int
    mouse_y: int
    box_x: int
    box_y: int

    def moved_to(self, mouse_x: int, mouse_y: int) -> tuple[int, int]:
        """鼠标移到 (mouse_x, mouse_y) 时箱子左上角的位置（还没夹进区域）。"""
        return self.box_x + mouse_x - self.mouse_x, self.box_y + mouse_y - self.mouse_y

    def far_enough(self, mouse_x: int, mouse_y: int) -> bool:
        dx, dy = abs(mouse_x - self.mouse_x), abs(mouse_y - self.mouse_y)
        return max(dx, dy) >= DRAG_THRESHOLD


# 连续的可见 ASCII 字符（如 3/4、#12）算一个词，不在中间断开；其余每个字符单独一段
_TOKENS = re.compile(r"\s+|[!-~]+|.")


def _wrap_line(text: str, max_width: int, measure: Measure) -> list[str]:
    lines: list[str] = []
    line = ""

    def push() -> None:
        nonlocal line
        lines.append(line.rstrip())
        line = ""

    for token in _TOKENS.findall(text):
        if measure(line + token) <= max_width:
            line += token
            continue
        if token.isspace():
            if line:
                push()
            continue
        if line:
            push()
        if measure(token) <= max_width:
            line = token
            continue
        for char in token:  # 一个词比一整行还长：按字符断开，每行至少一个字符
            if line and measure(line + char) > max_width:
                push()
            line += char
    if line or not lines:
        lines.append(line.rstrip())
    return lines


def clip(lines: list[str], max_lines: int, max_width: int, measure: Measure) -> list[str]:
    """只留前 max_lines 行；截掉了内容时最后一行末尾加"…"（放不下就再去掉几个字）。"""
    if len(lines) <= max_lines:
        return lines
    kept = lines[:max_lines]
    last = kept[-1]
    while last and measure(last + "…") > max_width:
        last = last[:-1]
    kept[-1] = last + "…"
    return kept


def wrap(text: str, max_width: int, measure: Measure) -> list[str]:
    """按像素宽度把文字断成多行（Tk 的 wraplength 对中文断行不可靠，所以自己断）。
    方案的各步之间用两个空格分隔，优先在步与步之间断开。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        line = ""
        for segment in raw.split(STEP_SEP):
            joined = f"{line}{STEP_SEP}{segment}" if line else segment
            if measure(joined) <= max_width:
                line = joined
                continue
            if line:
                lines.append(line)
            parts = _wrap_line(segment, max_width, measure)
            lines.extend(parts[:-1])
            line = parts[-1]
        lines.append(line)
    return lines
