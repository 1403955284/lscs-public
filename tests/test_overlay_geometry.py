import pytest

from lscs.overlay.geometry import (
    CORNER,
    PRESETS,
    Anchor,
    Drag,
    Layout,
    Rect,
    anchor_from_rect,
    clip,
    dock,
    font_px,
    grip_size,
    keep_inside,
    panel_layout,
    split_box,
    wrap,
)

SCREEN = Rect(0, 0, 1920, 1080)
CLIENT = Rect(0, 0, 1920, 1080)


def test_font_size_is_auto_from_client_height_and_clamped():
    assert font_px(1369) == 20  # 1440p 的炉石窗口（客户区高 1369）：1369 / 70 ≈ 19.6
    assert font_px(1080) == 15  # 1080 / 70 ≈ 15.4
    assert font_px(1440) == 21
    assert font_px(2160) == 31  # 4K：上限随高度变大（max(22, 2160 / 45)），不会被卡在 22
    assert font_px(900) == 14  # 12.9 → 下限 14
    assert font_px(800) == 14
    assert font_px(600) == 14
    assert font_px(400) == 14
    assert font_px(1080, font_size=30) == 30  # 手动字号不受限制


def test_panel_width_follows_font_size():
    # 约 22 个汉字 + 标签列 + 左右内边距
    layout = panel_layout(CLIENT)
    assert (layout.font_px, layout.pad, layout.width) == (15, 9, 368 + 2 * 9)  # 15 × 24.5 ≈ 368
    hd = panel_layout(Rect(0, 0, 2560, 1369))  # 1440p：面板约 514 像素宽（原来 642）
    assert (hd.font_px, hd.pad, hd.width) == (20, 12, 20 * 24.5 + 24)
    big = panel_layout(Rect(0, 0, 3840, 2160))
    assert (big.font_px, big.width) == (31, 760 + 2 * 19)  # 4K 下面板跟着变宽
    manual = panel_layout(CLIENT, font_size=16)
    assert (manual.font_px, manual.pad, manual.width) == (16, 10, 392 + 20)
    # 手动大字号也会加宽，但不超过客户区宽度的 35%
    assert panel_layout(CLIENT, font_size=30).width == 672
    assert panel_layout(Rect(0, 0, 800, 600)).width == 280  # 800 × 35%
    assert panel_layout(Rect(0, 0, 300, 200)).width == 160  # 再小也有个下限


def test_panel_height_is_capped():
    assert panel_layout(CLIENT).max_height == 486  # 1080 × 45%
    assert panel_layout(Rect(0, 0, 800, 600)).max_height == 270


def test_dock_presets():
    client = Rect(100, 50, 1600, 900)
    # 右下角：距客户区右边 2%、底边 2%，向上长（不挡"开始战斗"按钮）
    assert dock(client, SCREEN, 400, 300, PRESETS["right"]) == (
        100 + 1600 - 32 - 400,
        50 + 882 - 300,
    )
    # 左下角对称
    assert dock(client, SCREEN, 400, 300, PRESETS["left"]) == (132, 632)
    # 顶部：水平居中，距顶部 2%（不变）
    assert dock(client, SCREEN, 400, 300, PRESETS["top"]) == (100 + 600, 50 + 18)


def test_dock_keeps_panel_on_screen():
    tiny = Rect(1800, 1000, 200, 150)  # 客户区一部分在屏幕外，而且比面板还小
    x, y = dock(tiny, SCREEN, 300, 200, PRESETS["right"])
    assert (x, y) == (1920 - 300, 1080 - 200)
    x, y = dock(Rect(-500, -300, 600, 400), SCREEN, 300, 200, PRESETS["left"])
    assert (x, y) == (0, 0)
    # 屏幕原点不是 (0, 0)（多显示器的虚拟屏幕）
    virtual = Rect(-1920, 0, 3840, 1080)
    assert dock(Rect(-1920, 0, 1920, 1080), virtual, 400, 300, PRESETS["left"]) == (
        -1920 + 38,
        1058 - 300,
    )


def test_dock_keeps_the_box_inside_the_area():
    area = Rect(100, 100, 1000, 800)
    middle = Anchor(0.5, 0.5, "left", "top")
    assert dock(area, SCREEN, 300, 200, middle) == (600, 500)
    assert dock(area, SCREEN, 600, 500, middle) == (1100 - 600, 900 - 500)  # 放不下：贴右下边
    # 区域比箱子还小：贴区域左上角
    assert dock(Rect(100, 100, 300, 200), SCREEN, 400, 300, middle) == (100, 100)
    # 客户区一部分在屏幕外：先夹进客户区，再夹到屏幕
    assert dock(Rect(1500, 600, 1000, 800), SCREEN, 300, 200, middle) == (1920 - 300, 1080 - 200)
    # 多显示器：左边的显示器坐标为负
    virtual = Rect(-1920, -200, 3840, 1280)
    assert dock(Rect(-1900, -150, 1000, 800), virtual, 300, 200, middle) == (-1400, 250)
    assert keep_inside(-5000, 5000, 300, 200, Rect(-1900, -150, 1000, 800), virtual) == (
        -1900,
        650 - 200,
    )


def test_panel_taller_than_screen_starts_at_top():
    assert dock(CLIENT, SCREEN, 400, 2000, PRESETS["right"])[1] == 0


def test_corner_is_top_right_of_screen():
    assert dock(SCREEN, SCREEN, 400, 300, CORNER) == (1920 - 38 - 400, 54)
    small = Rect(0, 0, 300, 200)
    assert dock(small, small, 400, 300, CORNER) == (0, 0)


def test_anchor_from_rect_keeps_the_nearest_edges():
    area = Rect(100, 50, 1600, 900)  # 中心 (900, 500)
    # 在右下：记右边和下边（放在下半边的面板向上长）
    assert anchor_from_rect(Rect(1200, 600, 400, 300), area) == Anchor(
        0.9375, 0.9444, "right", "bottom"
    )
    # 在左上：记左边和上边
    assert anchor_from_rect(Rect(200, 100, 400, 300), area) == Anchor(0.0625, 0.0556, "left", "top")
    # 正好居中算右、下
    assert anchor_from_rect(Rect(700, 350, 400, 300), area) == Anchor(
        0.625, 0.6667, "right", "bottom"
    )
    # 比例夹到 0~1
    assert anchor_from_rect(Rect(1500, 800, 400, 300), area) == Anchor(1.0, 1.0, "right", "bottom")
    assert anchor_from_rect(Rect(0, 0, 100, 100), area) == Anchor(0.0, 0.0, "left", "top")


@pytest.mark.parametrize(
    "box",
    [
        Rect(1200, 600, 400, 300),
        Rect(200, 100, 400, 300),
        Rect(700, 350, 400, 300),
        Rect(100, 50, 669, 648),
        Rect(1031, 302, 669, 648),
        Rect(123, 457, 211, 99),
    ],
)
def test_anchor_from_rect_round_trips_through_dock(box):
    area = Rect(100, 50, 1600, 900)
    anchor = anchor_from_rect(box, area)
    assert dock(area, SCREEN, box.width, box.height, anchor) == (box.x, box.y)
    big = Rect(0, 0, 2560, 1440)
    anchor = anchor_from_rect(box, big)
    assert dock(big, big, box.width, box.height, anchor) == (box.x, box.y)


def test_grip_size_and_split_box():
    layout = Layout(width=669, font_px=26, pad=16, max_height=648)
    assert grip_size(layout) == (117, 36)  # 4.5 × 1.4 个字
    panel, grip = split_box(10, 20, 400, 300, 117, 36)
    assert grip == Rect(10 + 400 - 117, 20, 117, 36)  # 贴在箱子右上角
    assert panel == Rect(10, 20 + 36, 400, 300)  # 面板在标签正下方，不重叠
    assert panel.y == grip.bottom and panel.right == grip.right


def test_drag_follows_the_mouse_after_a_threshold():
    drag = Drag(mouse_x=100, mouse_y=200, box_x=10, box_y=20)
    assert drag.moved_to(300, 100) == (210, -80)
    assert drag.moved_to(100, 200) == (10, 20)
    assert not drag.far_enough(100, 200)
    assert not drag.far_enough(102, 198)  # 点一下手抖：不算拖
    assert drag.far_enough(103, 200)
    assert drag.far_enough(100, 197)


def measure(text):
    return 10 * len(text)


@pytest.mark.parametrize(
    ("text", "width", "lines"),
    [
        ("① 买 蚀脑怪 3/4  ② 刷新", 1000, ["① 买 蚀脑怪 3/4  ② 刷新"]),
        ("① 买 蚀脑怪 3/4  ② 刷新", 110, ["① 买 蚀脑怪 3/4", "② 刷新"]),  # 行尾空格丢掉
        ("① 买 蚀脑怪 3/4  ② 刷新", 90, ["① 买 蚀脑怪", "3/4  ② 刷新"]),  # 不把 3/4 拆开
        ("① 买 蚀脑怪 3/4  ② 刷新", 140, ["① 买 蚀脑怪 3/4", "② 刷新"]),  # 优先在两步之间断开
        ("思路：先保血再升本然后找海盗", 60, ["思路：先保血", "再升本然后找", "海盗"]),
        ("abcdefgh", 30, ["abc", "def", "gh"]),  # 一个词比一行还长时按字符断开
        ("", 100, [""]),
        ("一\n二", 100, ["一", "二"]),
    ],
)
def test_wrap(text, width, lines):
    assert wrap(text, width, measure) == lines


def test_wrap_puts_at_least_one_char_per_line():
    assert wrap("很长", 5, measure) == ["很", "长"]


@pytest.mark.parametrize(
    ("text", "width", "lines"),
    [
        # 名字之间（" · "）优先断开，断开处的分隔符不写；名字里的"·"不算
        ("凯瑞尔·罗姆 10 · 泰瑟兰·血望者 18", 150, ["凯瑞尔·罗姆 10", "泰瑟兰·血望者 18"]),
        (
            "★▲邪恶试验 · 多彩罗盘 · 操纵者肖像 · 代位神主",
            230,
            ["★▲邪恶试验 · 多彩罗盘 · 操纵者肖像", "代位神主"],
        ),
        ("甲 · 乙 · 丙", 1000, ["甲 · 乙 · 丙"]),  # 放得下就不断
        ("甲 · 乙丙丁戊己", 30, ["甲", "乙丙丁", "戊己"]),  # 一个名字比一行还长：照旧按字断开
        ("① 买 甲  ② 买 乙 · 丙", 100, ["① 买 甲", "② 买 乙 · 丙"]),  # 步与步之间照旧优先
        (
            "下个对手 洛卡拉 身材 12 · 最强 厄祖玛特 身材 194",
            200,
            ["下个对手 洛卡拉 身材 12", "最强 厄祖玛特 身材 194"],
        ),
    ],
)
def test_wrap_prefers_breaking_between_names(text, width, lines):
    assert wrap(text, width, measure) == lines


def test_wrap_can_ignore_name_separators():
    # 只留第一行的地方（画面上的战斗结果横幅）：第一行尽量写满
    text = "本场已定：赢 · 洛卡拉掉 5 血"
    assert wrap(text, 100, measure, names=False) == ["本场已定：赢 · 洛", "卡拉掉 5 血"]
    assert wrap(text, 100, measure) == ["本场已定：赢", "洛卡拉掉 5 血"]


def test_clip_keeps_first_lines_and_marks_the_cut():
    assert clip(["一二三", "四五"], 2, 100, measure) == ["一二三", "四五"]
    assert clip(["一二三", "四五", "六"], 1, 100, measure) == ["一二三…"]
    assert clip(["一二三四五", "六"], 1, 50, measure) == ["一二三四…"]  # 加"…"后放不下就再去一个字
