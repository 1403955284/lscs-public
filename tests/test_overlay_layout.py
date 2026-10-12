"""画面高亮的布局模型：内容区、槽位和按钮的位置。"""

import pytest

from lscs.config import HighlightConfig
from lscs.overlay.geometry import Rect
from lscs.overlay.layout import (
    ButtonSpec,
    Frame,
    HighlightLayout,
    RowSpec,
    button_rect,
    calibration_frames,
    content_rect,
    parameter_lines,
    slot_rect,
    step_frames,
)
from lscs.overlay.model import NextStep, Target

LAYOUT = HighlightLayout()
FULL_HD = Rect(0, 0, 1920, 1080)


def center(rect):
    return (rect.x + rect.width / 2, rect.y + rect.height / 2)


@pytest.mark.parametrize(
    ("client", "expected"),
    [
        (Rect(100, 50, 1920, 1080), Rect(100, 50, 1920, 1080)),  # 16:9：就是客户区
        (Rect(0, 0, 2560, 1080), Rect(320, 0, 1920, 1080)),  # 21:9：左右留边
        (Rect(0, 0, 1600, 1200), Rect(0, 150, 1600, 900)),  # 4:3：上下留边
    ],
)
def test_content_rect(client, expected):
    assert content_rect(client, 16 / 9) == expected


def test_a_row_is_centered():
    one = slot_rect(LAYOUT, "shop", 0, 1, FULL_HD)
    assert center(one) == pytest.approx((960, 0.31 * 1080), abs=1)
    assert (one.width, one.height) == (135, 189)  # 0.125 × 1080、0.175 × 1080
    four = [center(slot_rect(LAYOUT, "board", i, 4, FULL_HD)) for i in range(4)]
    pitch = 0.143 * 1080
    assert [x for x, _ in four] == pytest.approx(
        [960 - 1.5 * pitch + i * pitch for i in range(4)], abs=1
    )
    assert {round(y) for _, y in four} == {round(0.585 * 1080)}
    seven = [center(slot_rect(LAYOUT, "shop", i, 7, FULL_HD))[0] for i in range(7)]
    assert seven[3] == pytest.approx(960, abs=1)
    assert seven[0] + seven[6] == pytest.approx(1920, abs=2)  # 以内容区中心对称


def test_rows_on_a_wide_screen_stay_in_the_content_area():
    wide = Rect(0, 0, 2560, 1080)
    assert center(slot_rect(LAYOUT, "shop", 3, 7, wide))[0] == pytest.approx(1280, abs=1)
    assert slot_rect(LAYOUT, "shop", 0, 7, wide).width == 135  # 尺寸跟着高度


def test_four_choices_use_the_choice4_row():
    three = slot_rect(LAYOUT, "choice", 0, 3, FULL_HD)
    four = slot_rect(LAYOUT, "choice", 0, 4, FULL_HD)
    assert three.width == round(0.33 * 1080) and four.width == round(0.28 * 1080)
    assert center(four)[0] == pytest.approx(960 - 1.5 * 0.34 * 1080, abs=1)


def test_buttons_are_circles_in_the_content_area():
    rect = button_rect(LAYOUT, "upgrade", FULL_HD)
    assert center(rect) == pytest.approx((0.19 * 1920, 0.30 * 1080), abs=1)
    assert rect.width == rect.height == round(2 * 0.05 * 1080)
    wide = button_rect(LAYOUT, "freeze", Rect(0, 0, 2560, 1080))
    assert center(wide)[0] == pytest.approx(320 + 0.81 * 1920, abs=1)


def test_from_config_overrides_the_defaults():
    assert HighlightLayout.from_config(HighlightConfig()) == LAYOUT
    cfg = HighlightConfig(shop_y=0.3, shop_pitch=0.15, hero_power=(0.6, 0.8, 0.05), aspect=2.0)
    layout = HighlightLayout.from_config(cfg)
    assert layout.shop == RowSpec(y=0.3, pitch=0.15, width=0.125, height=0.175)
    assert layout.hero_power == ButtonSpec(x=0.6, y=0.8, radius=0.05)
    assert (layout.aspect, layout.board) == (2.0, LAYOUT.board)


def test_step_frames_draw_only_the_current_target():
    buy = NextStep("▶ 买", Target(row="shop", index=1, count=3), badge="2")
    assert step_frames(LAYOUT, buy, FULL_HD) == [
        Frame(slot_rect(LAYOUT, "shop", 1, 3, FULL_HD), label="2", badge=True)
    ]
    button = NextStep("▶ 刷新商店", Target(button="refresh"), badge="1")
    assert step_frames(LAYOUT, button, FULL_HD) == [
        Frame(button_rect(LAYOUT, "refresh", FULL_HD), round=True, label="1", badge=True)
    ]
    assert step_frames(LAYOUT, NextStep("▶ 等建议…", tone="muted"), FULL_HD) == []
    assert step_frames(LAYOUT, None, FULL_HD) == []
    empty_row = NextStep("▶ 买", Target(row="shop", index=0, count=0))
    assert step_frames(LAYOUT, empty_row, FULL_HD) == []


def test_calibration_frames_cover_every_slot_and_button():
    frames = calibration_frames(LAYOUT, FULL_HD)
    labels = [f.label for f in frames]
    assert labels == [
        *(f"商店{i}" for i in range(1, 8)),
        *(f"场面{i}" for i in range(1, 8)),
        *(f"选项{i}" for i in range(1, 4)),
        *(f"饰品{i}" for i in range(1, 5)),
        *(f"英雄{i}" for i in range(1, 5)),
        "升本",
        "刷新",
        "冻结",
        "技能",
    ]
    assert [f.round for f in frames].count(True) == 4
    assert not any(f.badge for f in frames)  # 标签写在框里，不画成徽章（"升本"也是两个字）
    assert [f.label for f in calibration_frames(LAYOUT, FULL_HD, ["buttons"])] == [
        "升本",
        "刷新",
        "冻结",
        "技能",
    ]


def test_parameter_lines_use_the_config_names():
    lines = parameter_lines(LAYOUT)
    assert lines[0] == "aspect = 1.7778"
    assert "shop_y = 0.31  shop_pitch = 0.143  shop_width = 0.125  shop_height = 0.175" in lines
    assert lines[-1] == "hero_power = [0.585, 0.82, 0.06]"
