import pytest

from lscs.overlay.geometry import Rect, clip, corner, dock, font_px, panel_layout, wrap

SCREEN = Rect(0, 0, 1920, 1080)
CLIENT = Rect(0, 0, 1920, 1080)


def test_font_size_is_auto_from_client_height_and_clamped():
    assert font_px(1080) == 20  # 1080 / 55 ≈ 19.6
    assert font_px(1440) == 26  # 上限随高度变大：max(22, 1440 / 45)
    assert font_px(2160) == 39  # 4K
    assert font_px(800) == 15
    assert font_px(400) == 12  # 7 → 下限 12
    assert font_px(1080, font_size=30) == 30  # 手动字号不受限制


def test_panel_width_follows_font_size():
    # 约 22 个汉字 + 标签列 + 左右内边距
    layout = panel_layout(CLIENT)
    assert (layout.font_px, layout.pad, layout.width) == (20, 12, 20 * 24.5 + 24)
    big = panel_layout(Rect(0, 0, 3840, 2160))
    assert (big.font_px, big.width) == (39, 956 + 2 * 23)  # 4K 下面板跟着变宽
    manual = panel_layout(CLIENT, font_size=16)
    assert (manual.font_px, manual.pad, manual.width) == (16, 10, 392 + 20)
    # 手动大字号也会加宽，但不超过客户区宽度的 35%
    assert panel_layout(CLIENT, font_size=30).width == 672
    assert panel_layout(Rect(0, 0, 800, 600)).width == 280  # 800 × 35%
    assert panel_layout(Rect(0, 0, 300, 200)).width == 160  # 再小也有个下限


def test_panel_height_is_capped():
    assert panel_layout(CLIENT).max_height == 486  # 1080 × 45%
    assert panel_layout(Rect(0, 0, 800, 600)).max_height == 270


def test_dock_right_left_top():
    client = Rect(100, 50, 1600, 900)
    # 右侧：距客户区右边 2%，距顶部 18%
    assert dock(client, SCREEN, 400, 300, "right") == (100 + 1600 - 32 - 400, 50 + 162)
    # 左侧对称
    assert dock(client, SCREEN, 400, 300, "left") == (100 + 32, 50 + 162)
    # 顶部：水平居中，距顶部 2%
    assert dock(client, SCREEN, 400, 300, "top") == (100 + 600, 50 + 18)


def test_dock_keeps_panel_on_screen():
    tiny = Rect(1800, 1000, 200, 150)  # 客户区一部分在屏幕外，而且比面板还小
    x, y = dock(tiny, SCREEN, 300, 200, "right")
    assert (x, y) == (1920 - 300, 1080 - 200)
    x, y = dock(Rect(-500, -300, 600, 400), SCREEN, 300, 200, "left")
    assert (x, y) == (0, 0)
    # 屏幕原点不是 (0, 0)（多显示器的虚拟屏幕）
    virtual = Rect(-1920, 0, 3840, 1080)
    assert dock(Rect(-1920, 0, 1920, 1080), virtual, 400, 300, "left") == (-1920 + 38, 194)


def test_panel_taller_than_screen_starts_at_top():
    assert dock(CLIENT, SCREEN, 400, 2000, "right")[1] == 0


def test_corner_is_top_right_of_screen():
    assert corner(SCREEN, 400, 300) == (1920 - 38 - 400, 54)
    assert corner(Rect(0, 0, 300, 200), 400, 300) == (0, 0)


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


def test_clip_keeps_first_lines_and_marks_the_cut():
    assert clip(["一二三", "四五"], 2, 100, measure) == ["一二三", "四五"]
    assert clip(["一二三", "四五", "六"], 1, 100, measure) == ["一二三…"]
    assert clip(["一二三四五", "六"], 1, 50, measure) == ["一二三四…"]  # 加"…"后放不下就再去一个字
