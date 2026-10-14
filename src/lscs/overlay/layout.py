"""画面高亮的布局模型（纯函数，任何平台都可以测试）：从炉石客户区算出每个槽位、按钮的位置。

炉石按高度缩放画面、内容区居中：先取客户区里最大的 aspect（默认 16:9）矩形作为内容区（宽屏左右
留边、窄屏上下留边），所有位置都在内容区里按比例算。x 用内容区宽度的比例，y 和尺寸用内容区高度的
比例（卡牌跟着高度缩放）。一排 count 个槽位以内容区中心对称排开：
中心 x = 内容区中心 + (index - (count - 1) / 2) × pitch × 高度。

商店、场面和四个按钮按用户的两张招募阶段截图（2560×1440、2559×1369）校准过（2026-10-12），饰品
（4 个选项）那一排按饰品商店的截图（2340×1301 客户区）校准过（2026-10-15）；3 个选项和选英雄两排
还是按 16:9 估计的。按钮半径为 0 表示还没校准、不画框。用 lscs highlight-test 对照截图
校准后改这里和 config.example.toml；config.toml 的 [highlight] 可以覆盖每一个数。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, fields
from typing import TYPE_CHECKING

from lscs.overlay.geometry import Rect

if TYPE_CHECKING:
    from lscs.config import HighlightConfig
    from lscs.overlay.model import NextStep, Target

ROWS = ("shop", "board", "choice", "choice4", "hero")  # highlight-test 能画的排
BUTTONS = ("upgrade", "refresh", "freeze", "hero_power")
CALIBRATION_ROWS = (*ROWS, "buttons")
# highlight-test 不加 --rows 时只画招募阶段的商店、场面和按钮，避免几排框叠在一起看不清
DEFAULT_CALIBRATION_ROWS = ("shop", "board", "buttons")
# highlight-test 每排画几个
CALIBRATION_SLOTS = {"shop": 7, "board": 7, "choice": 3, "choice4": 4, "hero": 4}
CALIBRATION_LABELS = {
    "shop": "商店",
    "board": "场面",
    "choice": "选项",
    "choice4": "饰品",
    "hero": "英雄",
    "upgrade": "升本",
    "refresh": "刷新",
    "freeze": "冻结",
    "hero_power": "技能",
}


@dataclass(frozen=True)
class RowSpec:
    y: float  # 这一排中心的高度比例
    pitch: float  # 相邻两个中心的距离（高度比例）
    width: float  # 一个槽位的宽（高度比例）
    height: float  # 一个槽位的高（高度比例）


@dataclass(frozen=True)
class ButtonSpec:
    x: float  # 中心，宽度比例
    y: float  # 中心，高度比例
    radius: float  # 高度比例


@dataclass(frozen=True)
class HighlightLayout:
    aspect: float = 16 / 9
    shop: RowSpec = RowSpec(y=0.367, pitch=0.128, width=0.11, height=0.15)
    board: RowSpec = RowSpec(y=0.55, pitch=0.128, width=0.11, height=0.15)
    choice: RowSpec = RowSpec(y=0.50, pitch=0.42, width=0.33, height=0.50)  # 2~3 个选项
    choice4: RowSpec = RowSpec(y=0.45, pitch=0.254, width=0.235, height=0.34)  # 4 个选项（饰品）
    hero: RowSpec = RowSpec(y=0.47, pitch=0.36, width=0.27, height=0.46)  # 选英雄
    upgrade: ButtonSpec = ButtonSpec(x=0.412, y=0.175, radius=0.05)
    refresh: ButtonSpec = ButtonSpec(x=0.588, y=0.175, radius=0.05)
    freeze: ButtonSpec = ButtonSpec(x=0.646, y=0.175, radius=0.05)
    hero_power: ButtonSpec = ButtonSpec(x=0.593, y=0.78, radius=0.055)

    @classmethod
    def from_config(cls, cfg: HighlightConfig) -> HighlightLayout:
        def row(name: str) -> RowSpec:
            return RowSpec(*(getattr(cfg, f"{name}_{f.name}") for f in fields(RowSpec)))

        return cls(
            aspect=cfg.aspect,
            **{name: row(name) for name in ROWS},
            **{name: ButtonSpec(*getattr(cfg, name)) for name in BUTTONS},
        )


@dataclass(frozen=True)
class Frame:
    """高亮要画的一个框：round 为 True 时是按钮（外接正方形），否则是槽位。"下一步"的框画成
    四角括号（[highlight] style = "frame" 时是整圈细线框）；校准框画完整的圆或圆角矩形。"""

    rect: Rect
    round: bool = False
    label: str = ""  # 徽章上的字（第几步、"选"），或 highlight-test 写在框里的标签（"商店1"）
    badge: bool = False  # True：label 画成框右上角上方的小圆徽章；False：写在框的正中间
    calibration: bool = False  # highlight-test 的校准框：完整的框，不呼吸


def content_rect(client: Rect, aspect: float) -> Rect:
    """客户区里最大的 aspect 矩形，居中。"""
    if client.width >= client.height * aspect:
        width = round(client.height * aspect)
        return Rect(client.x + (client.width - width) // 2, client.y, width, client.height)
    height = round(client.width / aspect)
    return Rect(client.x, client.y + (client.height - height) // 2, client.width, height)


def _row_spec(layout: HighlightLayout, row: str, count: int) -> RowSpec:
    if row == "choice" and count >= 4:
        return layout.choice4
    return getattr(layout, row)


def _box(cx: float, cy: float, width: float, height: float) -> Rect:
    return Rect(round(cx - width / 2), round(cy - height / 2), round(width), round(height))


def slot_rect(layout: HighlightLayout, row: str, index: int, count: int, client: Rect) -> Rect:
    """一排 count 个、从左数第 index 个（0 起）槽位的矩形；choice 有 4 个时用 choice4。"""
    content = content_rect(client, layout.aspect)
    spec = _row_spec(layout, row, count)
    h = content.height
    cx = content.x + content.width / 2 + (index - (count - 1) / 2) * spec.pitch * h
    return _box(cx, content.y + spec.y * h, spec.width * h, spec.height * h)


def button_calibrated(layout: HighlightLayout, button: str) -> bool:
    """半径为 0 表示这个按钮的位置还没校准，不画框。"""
    spec: ButtonSpec = getattr(layout, button)
    return spec.radius > 0


def button_rect(layout: HighlightLayout, button: str, client: Rect) -> Rect:
    """按钮外接正方形（画成圆）。"""
    content = content_rect(client, layout.aspect)
    spec: ButtonSpec = getattr(layout, button)
    h = content.height
    diameter = 2 * spec.radius * h
    return _box(content.x + spec.x * content.width, content.y + spec.y * h, diameter, diameter)


def target_frame(layout: HighlightLayout, target: Target, client: Rect, label: str = "") -> Frame:
    """画面高亮里"下一步"的框；label 是徽章上的字（第几步、"选"）。"""
    badge = bool(label)
    if target.button is not None:
        rect = button_rect(layout, target.button, client)
        return Frame(rect, round=True, label=label, badge=badge)
    assert target.row is not None
    rect = slot_rect(layout, target.row, target.index, target.count, client)
    return Frame(rect, label=label, badge=badge)


def step_frames(layout: HighlightLayout, step: NextStep | None, client: Rect) -> list[Frame]:
    """画面高亮为"下一步"要画的框：只画当前这一步；没有目标（等待、做完了）时什么都不画。"""
    if step is None or step.target is None:
        return []
    target = step.target
    if target.button is None and (target.row is None or target.count <= 0):
        return []
    if target.button is not None and not button_calibrated(layout, target.button):
        return []  # 这个按钮的位置还没校准，宁可不画也不画错地方
    return [target_frame(layout, target, client, step.badge)]


def calibration_frames(
    layout: HighlightLayout, client: Rect, rows: Iterable[str] = CALIBRATION_ROWS
) -> list[Frame]:
    """lscs highlight-test：每一排的每个槽位和四个按钮各一个框，框里写标签。"""
    frames: list[Frame] = []
    for row in rows:
        if row == "buttons":
            frames += [
                Frame(
                    button_rect(layout, b, client),
                    round=True,
                    label=CALIBRATION_LABELS[b],
                    calibration=True,
                )
                for b in BUTTONS
                if button_calibrated(layout, b)
            ]
            continue
        count = CALIBRATION_SLOTS[row]
        base = "choice" if row == "choice4" else row
        label = CALIBRATION_LABELS[row]
        frames += [
            Frame(
                slot_rect(layout, base, i, count, client),
                label=f"{label}{i + 1}",
                calibration=True,
            )
            for i in range(count)
        ]
    return frames


def parameter_lines(layout: HighlightLayout) -> list[str]:
    """highlight-test 打印的参数（和 config.toml 的 [highlight] 同名）。"""
    lines = [f"aspect = {layout.aspect:.4f}"]
    for row in ROWS:
        spec: RowSpec = getattr(layout, row)
        lines.append(
            f"{row}_y = {spec.y:g}  {row}_pitch = {spec.pitch:g}  "
            f"{row}_width = {spec.width:g}  {row}_height = {spec.height:g}"
        )
    for button in BUTTONS:
        spec_b: ButtonSpec = getattr(layout, button)
        lines.append(f"{button} = [{spec_b.x:g}, {spec_b.y:g}, {spec_b.radius:g}]")
    return lines
