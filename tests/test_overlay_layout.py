"""画面高亮的布局模型：内容区、槽位和按钮的位置。"""

import pytest

from lscs.config import HighlightConfig
from lscs.overlay.geometry import Rect
from lscs.overlay.layout import (
    BUTTONS,
    DEFAULT_CALIBRATION_ROWS,
    ButtonSpec,
    Frame,
    HighlightLayout,
    RowSpec,
    button_calibrated,
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
    assert center(one) == pytest.approx((960, 0.367 * 1080), abs=1)
    assert (one.width, one.height) == (119, 162)  # 0.11 × 1080 = 118.8、0.15 × 1080
    four = [center(slot_rect(LAYOUT, "board", i, 4, FULL_HD)) for i in range(4)]
    pitch = 0.128 * 1080
    assert [x for x, _ in four] == pytest.approx(
        [960 - 1.5 * pitch + i * pitch for i in range(4)], abs=1
    )
    assert {round(y) for _, y in four} == {round(0.55 * 1080)}
    seven = [center(slot_rect(LAYOUT, "shop", i, 7, FULL_HD))[0] for i in range(7)]
    assert seven[3] == pytest.approx(960, abs=1)
    assert seven[0] + seven[6] == pytest.approx(1920, abs=2)  # 以内容区中心对称


def test_rows_on_a_wide_screen_stay_in_the_content_area():
    wide = Rect(0, 0, 2560, 1080)
    assert center(slot_rect(LAYOUT, "shop", 3, 7, wide))[0] == pytest.approx(1280, abs=1)
    assert slot_rect(LAYOUT, "shop", 0, 7, wide).width == 119  # 尺寸跟着高度


def test_four_choices_use_the_choice4_row():
    three = slot_rect(LAYOUT, "choice", 0, 3, FULL_HD)
    four = slot_rect(LAYOUT, "choice", 0, 4, FULL_HD)
    assert three.width == round(0.33 * 1080) and four.width == round(0.28 * 1080)
    assert center(four)[0] == pytest.approx(960 - 1.5 * 0.34 * 1080, abs=1)


def test_buttons_are_circles_in_the_content_area():
    rect = button_rect(LAYOUT, "upgrade", FULL_HD)
    assert center(rect) == pytest.approx((0.412 * 1920, 0.175 * 1080), abs=1)
    assert rect.width == rect.height == round(2 * 0.05 * 1080)
    wide = button_rect(LAYOUT, "freeze", Rect(0, 0, 2560, 1080))
    assert center(wide) == pytest.approx((320 + 0.646 * 1920, 0.175 * 1080), abs=1)
    power = button_rect(LAYOUT, "hero_power", Rect(0, 0, 2560, 1080))
    assert center(power) == pytest.approx((320 + 0.593 * 1920, 0.78 * 1080), abs=1)
    assert power.width == power.height == round(2 * 0.055 * 1080)


# 半径 0：这个按钮还没校准（默认的四个按钮都校准过了，所以用显式的配置）
UNCALIBRATED = HighlightLayout.from_config(
    HighlightConfig(upgrade=(0.412, 0.175, 0.0), freeze=(0.646, 0.175, 0.0))
)


def test_buttons_with_radius_zero_are_not_calibrated():
    assert [b for b in BUTTONS if button_calibrated(LAYOUT, b)] == list(BUTTONS)
    assert [b for b in BUTTONS if button_calibrated(UNCALIBRATED, b)] == ["refresh", "hero_power"]


def test_from_config_overrides_the_defaults():
    assert HighlightLayout.from_config(HighlightConfig()) == LAYOUT
    cfg = HighlightConfig(shop_y=0.3, shop_pitch=0.15, hero_power=(0.6, 0.8, 0.05), aspect=2.0)
    layout = HighlightLayout.from_config(cfg)
    assert layout.shop == RowSpec(y=0.3, pitch=0.15, width=0.11, height=0.15)
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
    assert not any(f.calibration for f in step_frames(LAYOUT, button, FULL_HD))
    assert step_frames(LAYOUT, NextStep("▶ 等建议…", tone="muted"), FULL_HD) == []
    assert step_frames(LAYOUT, None, FULL_HD) == []
    empty_row = NextStep("▶ 买", Target(row="shop", index=0, count=0))
    assert step_frames(LAYOUT, empty_row, FULL_HD) == []


def test_position_step_frames_the_minion_to_drag():
    from lscs.overlay.model import BADGE_MOVE

    target = Target(row="board", index=1, count=6)
    drag = NextStep("▶ 把 左数第 2 个 孢林卵怪 拖到最左边", target, badge=BADGE_MOVE)
    assert step_frames(LAYOUT, drag, FULL_HD) == [
        Frame(slot_rect(LAYOUT, "board", 1, 6, FULL_HD), label="拖", badge=True)
    ]


@pytest.mark.parametrize("button", BUTTONS)
def test_uncalibrated_buttons_are_not_framed_until_given_a_radius(button):
    step = NextStep("▶ 按钮", Target(button=button), badge="1")
    x, y, _radius = getattr(HighlightConfig(), button)
    unset = HighlightLayout.from_config(HighlightConfig(**{button: (x, y, 0.0)}))
    assert step_frames(unset, step, FULL_HD) == []  # 宁可不画也不画错地方
    radius = getattr(LAYOUT, button).radius
    assert radius > 0  # 默认值都校准过：画框
    assert step_frames(LAYOUT, step, FULL_HD) == [
        Frame(button_rect(LAYOUT, button, FULL_HD), round=True, label="1", badge=True)
    ]
    assert button_rect(LAYOUT, button, FULL_HD).width == round(2 * radius * 1080)


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
    assert all(f.calibration for f in frames)  # 校准框画完整的框，不画四角括号、不呼吸
    assert len(frames) == 29


def test_calibration_buttons_skip_the_uncalibrated_ones():
    buttons = calibration_frames(LAYOUT, FULL_HD, ("buttons",))
    assert [f.label for f in buttons] == ["升本", "刷新", "冻结", "技能"]
    assert [f.rect for f in buttons] == [button_rect(LAYOUT, b, FULL_HD) for b in BUTTONS]
    assert all(f.round for f in buttons)
    unset = calibration_frames(UNCALIBRATED, FULL_HD, ("buttons",))
    assert [f.label for f in unset] == ["刷新", "技能"]  # 升本、冻结的半径是 0，不画
    assert [f.rect for f in unset] == [
        button_rect(UNCALIBRATED, "refresh", FULL_HD),
        button_rect(UNCALIBRATED, "hero_power", FULL_HD),
    ]


def test_default_calibration_rows_are_the_recruit_screen():
    assert DEFAULT_CALIBRATION_ROWS == ("shop", "board", "buttons")
    labels = [f.label for f in calibration_frames(LAYOUT, FULL_HD, DEFAULT_CALIBRATION_ROWS)]
    assert labels == [
        *(f"商店{i}" for i in range(1, 8)),
        *(f"场面{i}" for i in range(1, 8)),
        "升本",
        "刷新",
        "冻结",
        "技能",
    ]


# 用户两张招募阶段截图上量的位置（2026-10-12，设计文档的"校准记录"）。默认值取两张的折中，
# 改默认值时这两个测试会提醒还对不对得上。


def test_calibrated_against_screenshot_1_2560x1440():
    """第一张：2560×1440 全屏，商店 6 个随从、场面 5 个随从的肖像中心。"""
    client = Rect(0, 0, 2560, 1440)
    assert content_rect(client, LAYOUT.aspect) == client  # 16:9：内容区就是整个画面
    shop = [center(slot_rect(LAYOUT, "shop", i, 6, client)) for i in range(6)]
    assert [x for x, _ in shop] == pytest.approx([823, 1004, 1189, 1371, 1557, 1745], abs=12)
    assert [y for _, y in shop] == pytest.approx([520] * 6, abs=15)
    board = [center(slot_rect(LAYOUT, "board", i, 5, client)) for i in range(5)]
    assert [x for x, _ in board] == pytest.approx([904, 1086, 1271, 1473, 1653], abs=12)
    assert [y for _, y in board] == pytest.approx([798] * 5, abs=15)
    frame = slot_rect(LAYOUT, "shop", 0, 6, client)
    assert frame.width >= 140 and frame.height >= 190  # 框住整个肖像（约 140×190）


def test_calibrated_against_screenshot_2_2559x1369():
    """第二张：2559×1369，内容区左右各留约 62 像素；商店 3 个、场面 6 个随从，三个按钮和英雄技能。"""
    client = Rect(0, 0, 2559, 1369)
    assert content_rect(client, LAYOUT.aspect) == Rect(62, 0, 2434, 1369)
    shop = [center(slot_rect(LAYOUT, "shop", i, 3, client)) for i in range(3)]
    assert [x for x, _ in shop] == pytest.approx([1106, 1281, 1460], abs=12)
    assert [y for _, y in shop] == pytest.approx([512] * 3, abs=15)
    board = [center(slot_rect(LAYOUT, "board", i, 6, client)) for i in range(6)]
    # 这一排的量值本身就不均匀（间距 189/154/189/160/187），所以放宽到 ±20
    assert [x for x, _ in board] == pytest.approx([836, 1025, 1179, 1368, 1528, 1715], abs=20)
    assert [y for _, y in board] == pytest.approx([746] * 6, abs=15)
    buttons = [center(button_rect(LAYOUT, b, client)) for b in ("upgrade", "refresh", "freeze")]
    assert buttons == [
        pytest.approx((1065, 240), abs=15),
        pytest.approx((1494, 240), abs=15),
        pytest.approx((1635, 240), abs=15),
    ]
    power = center(button_rect(LAYOUT, "hero_power", client))
    assert power == pytest.approx((1493, 1077), abs=20)


def test_parameter_lines_use_the_config_names():
    lines = parameter_lines(LAYOUT)
    assert lines[0] == "aspect = 1.7778"
    assert "shop_y = 0.367  shop_pitch = 0.128  shop_width = 0.11  shop_height = 0.15" in lines
    assert "board_y = 0.55  board_pitch = 0.128  board_width = 0.11  board_height = 0.15" in lines
    assert "upgrade = [0.412, 0.175, 0.05]" in lines
    assert lines[-1] == "hero_power = [0.593, 0.78, 0.055]"
    assert "freeze = [0.646, 0.175, 0]" in parameter_lines(UNCALIBRATED)  # 半径 0：还没校准
