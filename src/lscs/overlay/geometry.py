"""纯函数：按炉石客户区和屏幕大小计算面板的位置、宽度、字号，以及文字换行。"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

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
SIDE_TOP = 0.18  # 左右位置：距客户区顶部 18% 高度
TOP_MARGIN = 0.02  # 顶部位置：距客户区顶部 2% 高度
CORNER_TOP = 0.05  # 没有炉石窗口可贴时放在屏幕右上角，距顶部 5% 高度（避开 Mac 菜单栏）

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


def _keep_on_screen(x: int, y: int, width: int, height: int, screen: Rect) -> tuple[int, int]:
    # 面板比屏幕还大时贴左上角
    x = max(screen.x, min(x, screen.right - width))
    y = max(screen.y, min(y, screen.bottom - height))
    return x, y


def dock(
    client: Rect, screen: Rect, width: int, height: int, position: Position
) -> tuple[int, int]:
    """面板左上角的屏幕坐标：贴在客户区的右侧、左侧或顶部，并且不超出屏幕。"""
    margin = round(client.width * SIDE_MARGIN)
    if position == "top":
        x = client.x + (client.width - width) // 2
        y = client.y + round(client.height * TOP_MARGIN)
    else:
        y = client.y + round(client.height * SIDE_TOP)
        x = client.x + margin if position == "left" else client.right - margin - width
    return _keep_on_screen(x, y, width, height, screen)


def corner(screen: Rect, width: int, height: int) -> tuple[int, int]:
    """屏幕右上角（不在 Windows 上、或者无法贴靠炉石窗口时使用）。"""
    x = screen.right - round(screen.width * SIDE_MARGIN) - width
    y = screen.y + round(screen.height * CORNER_TOP)
    return _keep_on_screen(x, y, width, height, screen)


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
