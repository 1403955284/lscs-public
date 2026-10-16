"""对手阵容卡图窗口的纯函数（对手阵容卡图窗口设计文档第 4、5.2、5.3 节）：行和卡图、排版、位置；
以及"对手"小标签的尺寸和箱子的拆分、焦点提醒的文案。"""

from dataclasses import replace

import pytest
from factory import make_state

from lscs.advisor.opponents import opponent_text, stats_text, summarize_opponents
from lscs.overlay import boards as b
from lscs.overlay.boards import (
    BoardRow,
    OpponentBoards,
    Portrait,
    boards_layout,
    boards_rect,
    boards_side,
    build_boards,
)
from lscs.overlay.geometry import Layout, Rect, box_width, split_box, tab_size
from lscs.state.models import Minion, OpponentView, Phase

NAMES = {
    "H2": "洛卡拉",
    "H3": "恐龙大师布莱恩",
    "H4": "古夫",
    "H5": "詹迪斯",
    "H6": "米尔豪斯",
    "H7": "厄祖玛特",
    "H8": "艾德温",
    "GOLD": "金牌猎手",
    "GOLD_G": "金牌猎手(金)",
    "SHIELD": "圣盾机器人",
    "KW": "关键词怪",
    "MEGA": "超级风怒",
    "STEALTHY": "潜行者",
}


def name(card_id):
    return NAMES.get(card_id, f"卡{card_id}")


def pawn(position, card_id="M", atk=2, health=3, golden=False, keywords=()):
    return Minion(
        entity_id=100 + position,
        card_id=card_id,
        position=position,
        tier=1,
        races=(),
        atk=atk,
        health=health,
        golden=golden,
        keywords=tuple(keywords),
    )


def opp(player_id, health, place, board=(), seen=None, armor=0, tier=4):
    return OpponentView(
        player_id=player_id,
        hero_card_id=f"H{player_id}",
        health=health,
        armor=armor,
        place=place,
        tavern_tier=tier,
        last_board=tuple(board),
        last_seen_turn=seen,
    )


# 第 1 名：8 个随从（多出 1 个截掉），位置乱序；有 _G 的金色、PREMIUM 的金色、圣盾 + 嘲讽；最强
BIG = [
    pawn(3, "SHIELD", 20, 20, keywords=("DIVINE_SHIELD", "TAUNT")),
    pawn(1, "GOLD_G", 30, 30),
    pawn(2, "GOLD", 25, 25, golden=True),
    pawn(8, "LAST", 1, 1),
    pawn(4, "M4", 10, 10),
    pawn(5, "M5", 10, 10),
    pawn(7, "M7", 10, 10),
    pawn(6, "M6", 10, 10),
]
# 第 5 名：关键词多于 3 个、超级风怒、潜行
KEYWORDS = [
    pawn(1, "KW", 3, 3, keywords=("WINDFURY", "REBORN", "VENOMOUS", "TAUNT", "STEALTH")),
    pawn(2, "MEGA", 2, 2, keywords=("MEGA_WINDFURY", "STEALTH")),
    pawn(3, "STEALTHY", 1, 1, keywords=("STEALTH", "POISONOUS")),
]
GHOST = [pawn(1, "G1", 2, 2), pawn(2, "G2", 3, 3)]


def opponents():
    return {
        2: opp(2, 30, 1, BIG, seen=9, armor=9, tier=5),
        3: opp(3, 28, 2),  # 没见过
        4: opp(4, 20, 3, (), seen=7),  # 见到空场
        5: opp(5, 0, 8, GHOST, seen=4),  # 已淘汰，下一个对手（残影）
        6: opp(6, -3, 7, [pawn(1)], seen=3),  # 已淘汰
        7: opp(7, 15, 4, [pawn(1, "M", 1, 1)], seen=8),
        8: opp(8, 6, 5, KEYWORDS, seen=6),
    }


def state(turn=10, phase=Phase.RECRUIT, next_id=5, opps=None):
    return make_state(turn=turn, phase=phase).model_copy(
        update={"opponents": opponents() if opps is None else opps, "next_opponent_id": next_id}
    )


def boards_of(s, game=1):
    return build_boards(s, summarize_opponents(s), name, game)


def test_rows_are_living_by_place_then_the_ghost_then_eliminated():
    s = state()
    result = boards_of(s, game=3)
    assert result.title == "对手阵容（上次交手时）· 第 10 回合"
    assert [r.player_id for r in result.rows] == [2, 3, 4, 7, 8, 5]
    assert result.eliminated == "已淘汰：米尔豪斯"  # 残影不算在里面
    assert result.note == "日志里只有交手时的阵容，对手之后可能变强了"
    assert result.game == 3
    overview = summarize_opponents(s)
    by_id = {o.player_id: o for o in overview.opponents}
    for row in result.rows:
        summary = by_id[row.player_id]
        expected = opponent_text(
            summary, name, 10, place=True, seen=True, threat=False, stats=False
        )
        assert row.text == expected  # 短写：阵容的大小单独放在 stats，放得下才画
        assert row.stats == stats_text(summary)
    # 标题行的短写一定带着"第 N 回合见"；阵容的大小在 stats 里
    assert result.rows[0].text == "第1名 洛卡拉 30+9 · 5本 · 第 9 回合见"
    assert result.rows[0].stats == "身材 232"
    assert [r.hero for r in result.rows] == [
        "洛卡拉",
        "恐龙大师布莱恩",
        "古夫",
        "厄祖玛特",
        "艾德温",
        "詹迪斯",
    ]
    assert result.rows[1].text == "第2名 恐龙大师布莱恩 28+0 · 4本 · 还没见过阵容"
    assert result.rows[2].text == "第3名 古夫 20+0 · 4本 · 第 7 回合见到空场"
    assert (result.rows[1].stats, result.rows[2].stats) == ("", "")
    assert result.rows[5].text == "第8名 詹迪斯 已淘汰（残影）· 第 4 回合见"
    assert result.rows[5].stats == "2 个 · 身材 10"


def test_short_opponent_text_only_drops_the_board_size():
    """stats=False 只省掉阵容的大小；面板用的完整写法（stats=True，默认）不变。"""
    overview = summarize_opponents(state())
    for s in overview.opponents:
        full = opponent_text(s, name, 10, place=True, seen=True, threat=False)
        short = opponent_text(s, name, 10, place=True, seen=True, threat=False, stats=False)
        size = stats_text(s)
        if size:
            assert short != full and size in full and size not in short
        else:
            assert short == full
    edwin = next(s for s in overview.opponents if s.player_id == 8)
    short = opponent_text(edwin, name, 10, place=True, seen=True, threat=True, stats=False)
    assert short == f"第5名 艾德温 6+0 · 4本 · 第 6 回合见 · 威胁 {edwin.threat}"


def test_labels_are_words_and_only_marked_rows_are_bright():
    rows = {r.player_id: r for r in boards_of(state()).rows}
    assert (rows[5].label, rows[5].tone) == ("下个对手", "text")  # 残影是下一个对手
    assert (rows[2].label, rows[2].tone) == ("最强", "text")
    assert all((rows[p].label, rows[p].tone) == ("", "dim") for p in (3, 4, 7, 8))
    result = boards_of(state(next_id=2))
    rows = {r.player_id: r for r in result.rows}
    assert rows[2].label == "下个对手·最强"
    assert 5 not in rows and result.eliminated == "已淘汰：米尔豪斯、詹迪斯"  # 按名次
    combat = {r.player_id: r for r in boards_of(state(phase=Phase.COMBAT, next_id=2)).rows}
    assert combat[2].label == "对手·最强"
    combat = {r.player_id: r for r in boards_of(state(phase=Phase.COMBAT, next_id=7)).rows}
    assert combat[7].label == "对手" and combat[2].label == "最强"


def test_no_arrow_or_star_symbols_anywhere():
    for s in (state(), state(next_id=2), state(phase=Phase.COMBAT, next_id=2)):
        result = boards_of(s)
        texts = [result.title, result.eliminated, result.note]
        texts += [t for r in result.rows for t in (r.label, r.text)]
        assert not any("▶" in t or "★" in t for t in texts)


def test_portraits_follow_position_up_to_seven():
    rows = {r.player_id: r for r in boards_of(state()).rows}
    big = rows[2].portraits
    assert [p.card_id for p in big] == ["GOLD_G", "GOLD", "SHIELD", "M4", "M5", "M6", "M7"]
    assert [(p.atk, p.health) for p in big[:3]] == [(30, 30), (25, 25), (20, 20)]
    assert rows[3].portraits == () and rows[4].portraits == ()  # 没见过、空场
    assert [p.card_id for p in rows[5].portraits] == ["G1", "G2"]  # 残影画它最后的阵容
    assert all(isinstance(p, Portrait) for p in big)


def test_golden_shield_names_and_marks():
    rows = {r.player_id: r for r in boards_of(state()).rows}
    gold_g, gold, shield = rows[2].portraits[:3]
    assert gold_g.golden and gold.golden and not shield.golden  # _G 后缀和 PREMIUM 都算
    assert gold_g.name == "金牌猎手"  # 去掉"(金)"：金色靠外圈
    assert gold.name == "金牌猎手"
    assert shield.shield and not gold.shield
    assert shield.marks == ("嘲",)
    kw, mega, stealthy = rows[8].portraits
    assert kw.marks == ("嘲", "毒", "复")  # 优先级 嘲 > 剧 / 毒 > 复 > 风，最多 3 个
    assert mega.marks == ("风",)  # 超级风怒也写"风"，潜行不画
    assert stealthy.marks == ("剧",)


def test_no_game_has_no_rows():
    for s in (
        make_state(turn=0, phase=Phase.WAITING),
        make_state(turn=0, phase=Phase.HERO_SELECT).model_copy(
            update={"opponents": opponents(), "next_opponent_id": 5}
        ),
        make_state(turn=2),  # 还没有对手的数据
        None,
    ):
        result = build_boards(s, None, name, 4)
        assert result.title == "还没有对局" and result.rows == ()
        assert result.note == "" and result.eliminated == "" and result.game == 4


def test_overview_is_computed_when_not_given():
    s = state()
    assert build_boards(s, None, name, 1) == boards_of(s)


def test_same_input_same_content_and_health_changes_it():
    first, again = boards_of(state()), boards_of(state())
    assert first == again
    changed = state(opps={**opponents(), 7: opp(7, 9, 4, [pawn(1, "M", 1, 1)], seen=8)})
    assert boards_of(changed) != first


def test_game_over_keeps_the_last_boards():
    over = boards_of(state(phase=Phase.GAME_OVER))
    assert over.title == "对手阵容（上次交手时）· 第 10 回合" and len(over.rows) == 6


# ---- 排版 ----


def full_boards(rows=7, portraits=7, eliminated="", note=b.NOTE):
    row = BoardRow(
        player_id=1,
        label="",
        text="第1名 洛卡拉 30+9",
        tone="dim",
        portraits=tuple(
            Portrait(f"C{i}", "名字", 1, 1, False, False, ()) for i in range(portraits)
        ),
    )
    return OpponentBoards(
        title="t",
        rows=tuple(replace(row, player_id=i + 1) for i in range(rows)),
        eliminated=eliminated,
        note=note,
        game=1,
        version=0,
    )


def test_layout_at_1440p_matches_the_mockup():
    """样稿按 1440p、字号 20 算的（真实的自动字号见下一个用例）。"""
    layout = boards_layout(Rect(0, 0, 2560, 1440), 20, full_boards())
    assert layout.width == 511 and layout.d == 64 and layout.oval_h == 80
    assert layout.badge_r == 13 and layout.pad == 12
    assert layout.title_px == 20 and layout.small_px == 17
    assert layout.height <= round(0.78 * 1440)
    assert len(layout.rows) == 7 and layout.overflow == 0 and layout.note_y is not None


def test_layout_at_1440p_with_the_real_auto_font():
    """全屏 1440p 的自动字号是 font_px(1440) = 21（不是样稿用的 20）：卡图 63×79。"""
    from lscs.overlay.geometry import font_px

    px = font_px(1440)
    layout = boards_layout(Rect(0, 0, 2560, 1440), px, full_boards())
    assert px == 21
    assert (layout.width, layout.d, layout.oval_h, layout.badge_r) == (511, 63, 79, 13)
    assert (layout.pad, layout.title_px, layout.small_px) == (13, 21, 18)
    assert layout.height == 1033 and len(layout.rows) == 7  # 0.72 H，上限 0.78 H
    window = boards_layout(Rect(0, 0, 2559, 1369), font_px(1369), full_boards())
    assert (window.width, window.d) == (486, 60)  # 窗口模式 1369：字号 20，floor 取 60


def test_photo_cache_holds_every_portrait_of_a_full_lobby():
    pytest.importorskip("tkinter")
    from lscs.overlay.portraits import MAX_PHOTOS

    assert b.MAX_OPPONENTS == 7
    assert MAX_PHOTOS >= b.SLOTS * b.MAX_OPPONENTS  # 一屏 49 张不会互相挤掉


def test_layout_for_the_fake_hearthstone_and_1080p():
    small = boards_layout(Rect(100, 50, 1600, 900), 14, full_boards())
    assert (small.width, small.d) == (320, 40)
    hd = boards_layout(Rect(0, 0, 1920, 1080), 15, full_boards())
    assert (hd.width, hd.d, hd.badge_r) == (383, 48, 10)


def test_layout_width_is_capped_by_the_client_width():
    four_three = boards_layout(Rect(0, 0, 1600, 1200), 17, full_boards())
    assert four_three.width == round(0.22 * 1600) - round(0.02 * 1600)  # 不按高度的 0.355


def test_slots_are_evenly_spaced_and_fit():
    for client, px in ((Rect(0, 0, 2560, 1440), 20), (Rect(0, 0, 1920, 1080), 15)):
        layout = boards_layout(client, px, full_boards())
        steps = {b2 - a for a, b2 in zip(layout.slots, layout.slots[1:], strict=False)}
        assert len(layout.slots) == 7 and len(steps) == 1
        assert steps.pop() == layout.d + layout.gap
        assert layout.slots[0] == layout.pad
        assert layout.slots[-1] + layout.d <= layout.width - layout.pad


def test_rows_without_portraits_are_short():
    layout = boards_layout(Rect(0, 0, 2560, 1440), 20, full_boards(rows=2, portraits=0))
    tall = boards_layout(Rect(0, 0, 2560, 1440), 20, full_boards(rows=2))
    assert layout.height < tall.height
    assert all(r.portrait_y is None for r in layout.rows)
    assert all(r.portrait_y is not None for r in tall.rows)
    first, second = tall.rows
    assert second.y - first.y == tall.small_h + round(b.ROW_EXTRA * tall.d)
    assert first.portrait_y - first.y == tall.small_h + round(0.25 * tall.d)


def test_too_tall_first_shrinks_the_portraits():
    shrunk = boards_layout(Rect(100, 50, 1600, 900), 22, full_boards())
    natural = int((shrunk.width - 2 * shrunk.pad) // 7.6)  # 按宽度本来能有多宽
    assert b.D_MIN <= shrunk.d < natural
    assert shrunk.height <= round(0.78 * 900)
    assert shrunk.note_y is not None and shrunk.overflow == 0 and len(shrunk.rows) == 7


def test_then_drops_the_note_then_rows_from_the_end():
    no_note = boards_layout(Rect(100, 50, 1600, 900), 30, full_boards())
    assert no_note.d == b.D_MIN and no_note.note_y is None
    assert len(no_note.rows) == 7 and no_note.overflow == 0
    assert no_note.height <= round(0.78 * 900)
    crowded = boards_layout(Rect(100, 50, 1600, 900), 40, full_boards(eliminated="已淘汰：甲"))
    assert crowded.d == b.D_MIN and crowded.note_y is None
    assert crowded.overflow > 0 and len(crowded.rows) + crowded.overflow == 7
    assert [r.index for r in crowded.rows] == list(range(len(crowded.rows)))  # 去掉的是后面的
    assert crowded.overflow_y is not None and crowded.eliminated_y is not None
    assert crowded.height <= round(0.78 * 900)
    tiny = boards_layout(Rect(0, 0, 400, 200), 40, full_boards())
    assert len(tiny.rows) == 1 and tiny.overflow == 6  # 标题、第一行永远保留


def test_eliminated_and_note_lines_are_placed_below_the_rows():
    layout = boards_layout(Rect(0, 0, 2560, 1440), 20, full_boards(eliminated="已淘汰：甲"))
    last = layout.rows[-1]
    assert layout.eliminated_y >= last.portrait_y + layout.oval_h
    assert layout.note_y == layout.eliminated_y + layout.small_h
    assert layout.height == layout.note_y + layout.small_h + layout.pad


# ---- 位置 ----

AREA = Rect(100, 50, 1600, 900)
SCREEN = Rect(0, 0, 1920, 1080)


def test_boards_go_left_by_default():
    layout = boards_layout(AREA, 14, full_boards())
    box = Rect(1000, 500, 400, 300)  # 面板在右下角
    assert boards_side(AREA, box) == "left"
    rect = boards_rect(AREA, SCREEN, box, layout, docked=True)
    assert (rect.x, rect.y) == (132, 158)
    assert (rect.width, rect.height) == (layout.width, layout.height)


def test_boards_go_right_when_the_panel_is_on_the_left():
    layout = boards_layout(AREA, 14, full_boards())
    box = Rect(132, 500, 400, 300)
    assert boards_side(AREA, box) == "right"
    rect = boards_rect(AREA, SCREEN, box, layout, docked=True)
    assert rect.right == 100 + 1600 - 32 and rect.y == 158
    centred = Rect(100 + 800 - 200, 60, 400, 100)  # 顶部预设：中心正好 0.5
    assert boards_side(AREA, centred) == "left"
    assert boards_rect(AREA, SCREEN, box, layout, docked=True, side="left").x == 132


def test_boards_are_kept_inside_the_client_and_the_screen():
    tall = replace(boards_layout(AREA, 14, full_boards()), height=950)
    rect = boards_rect(AREA, SCREEN, Rect(1000, 500, 400, 300), tall, docked=True)
    assert rect.y == AREA.y  # 比客户区还高：贴客户区顶边
    off = Rect(-300, 50, 1600, 900)  # 客户区一部分在屏幕外
    layout = boards_layout(off, 14, full_boards())
    rect = boards_rect(off, SCREEN, Rect(800, 500, 400, 300), layout, docked=True)
    assert rect.x == 0


def test_preview_puts_the_boards_left_of_the_box():
    layout = boards_layout(SCREEN, 15, full_boards())
    box = Rect(1400, 54, 500, 300)
    rect = boards_rect(SCREEN, SCREEN, box, layout, docked=False)
    assert rect.right == box.x - round(0.5 * 15) and rect.y == box.y
    narrow = boards_rect(SCREEN, SCREEN, Rect(100, 54, 500, 300), layout, docked=False)
    assert narrow.x == 0 and narrow.y == 54  # 放不下：贴屏幕左边


# ---- "对手"小标签 ----


def test_tab_size_and_the_three_way_split():
    layout = Layout(width=669, font_px=20, pad=12, max_height=648)
    assert tab_size(layout) == (64, 28)  # 3.2 × 1.4 个字
    gap = round(0.3 * 20)
    panel, grip, tab = split_box(10, 20, 400, 300, 90, 28, 64, gap)
    assert grip == Rect(10 + 400 - 90, 20, 90, 28)  # 拖动标签仍贴右上角
    assert tab == Rect(grip.x - gap - 64, 20, 64, 28)  # "对手"在它左边，同一行、同高
    assert panel == Rect(10, 48, 400, 300)
    assert tab.right < grip.x and panel.y == tab.bottom == grip.bottom


def test_tab_alone_takes_the_corner_and_no_tab_is_the_old_split():
    panel, _grip, tab = split_box(10, 20, 400, 300, 0, 28, 64, 6)
    assert tab == Rect(10 + 400 - 64, 20, 64, 28) and panel.y == 48
    panel, grip, tab = split_box(10, 20, 400, 300, 117, 36)
    assert grip == Rect(10 + 400 - 117, 20, 117, 36) and panel == Rect(10, 56, 400, 300)
    assert tab.width == 0


def test_box_is_never_narrower_than_both_tags():
    assert box_width(50, 90, 64, 6) == 160
    assert box_width(500, 90, 64, 6) == 500
    assert box_width(50, 0, 64, 6) == 64  # 没有拖动标签：不留间隔
    assert box_width(50, 90, 0, 6) == 90


@pytest.mark.parametrize(
    ("drag_handle", "opponent_boards", "switches"),
    [
        (True, True, "drag_handle 和 opponent_boards"),
        (True, False, "drag_handle"),
        (False, True, "opponent_boards"),
    ],
)
def test_focus_lost_text_names_the_switches_that_are_on(drag_handle, opponent_boards, switches):
    pytest.importorskip("tkinter")
    from lscs.overlay.window import focus_lost_text

    assert focus_lost_text(drag_handle, opponent_boards) == (
        "注意：炉石失去了焦点（前台是 lscs 的面板）；"
        f"如果反复出现，请把 [overlay] {switches} 设为 false 并告诉开发者"
    )


def test_focus_lost_text_without_any_tag():
    pytest.importorskip("tkinter")
    from lscs.overlay.window import FOCUS_LOST, FOCUS_LOST_NO_GRIP, focus_lost_text

    assert focus_lost_text(False, False) == FOCUS_LOST_NO_GRIP
    assert focus_lost_text(True, False) == FOCUS_LOST


def test_oval_portrait_is_a_4_by_5_crop_with_transparent_corners():
    pytest.importorskip("tkinter")
    from PIL import Image

    from lscs.overlay.portraits import oval_portrait

    art = Image.new("RGB", (256, 256), (10, 20, 30))
    for x in range(256):  # 左右两边涂红：裁成 4:5 时被裁掉
        for y in range(256):
            if x < 20 or x >= 236:
                art.putpixel((x, y), (255, 0, 0))
    portrait = oval_portrait(art, 64)
    assert portrait.mode == "RGBA" and portrait.size == (64, 80)
    assert portrait.getpixel((0, 0))[3] == 0 and portrait.getpixel((63, 79))[3] == 0
    assert portrait.getpixel((32, 40)) == (10, 20, 30, 255)
    assert portrait.getpixel((2, 40))[0] < 100  # 红边被裁掉了
    tall = oval_portrait(Image.new("RGB", (100, 300), (1, 2, 3)), 40)
    assert tall.size == (40, 50)
