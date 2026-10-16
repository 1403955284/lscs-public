"""对手阵容卡图窗口的内容和排版（对手阵容卡图窗口设计文档第 4、5.2、5.3 节）：纯函数，不碰 Tk。

- build_boards：每个对手一行（标题行 + 最多 7 个卡图），只画日志里有的——每个对手**最近一次和你
  交手时**的阵容；标记用文字（"下个对手" / "对手" / "最强"），不用 ▶ / ★（面板上 ▶ 只给
  "下一步"、★ 只给推荐的选项和英雄，卡图窗口和面板同屏，也遵守）；
- boards_layout：窗口和卡图的尺寸随炉石客户区的高度缩放，再按宽度封顶；太高时先缩卡图、再去附注、
  再从最后一行起去掉对手行；
- boards_rect：默认贴在客户区左侧（排行榜那一条，没有可点的东西），面板在左半边时换到右侧；
  Mac 预览和退路时放在面板左边。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from lscs.advisor.context import is_golden
from lscs.advisor.opponents import (
    Namer,
    OpponentOverview,
    eliminated_names,
    opponent_text,
    stats_text,
    summarize_opponents,
)
from lscs.overlay.geometry import PAD_RATIO, SIDE_MARGIN, Rect, keep_inside
from lscs.state.models import GameState, Minion, Phase

Tone = Literal["text", "dim", "muted"]
Side = Literal["left", "right"]

TITLE = "对手阵容（上次交手时）· 第 {turn} 回合"
STATS_SEP = " · "  # 标题行的短写和阵容大小之间
NO_GAME = "还没有对局"
NOTE = "日志里只有交手时的阵容，对手之后可能变强了"
ELIMINATED = "已淘汰："
OVERFLOW = "还有 {count} 个对手放不下"
NEXT = "下个对手"
FIGHTING = "对手"  # 战斗中：正在打的对手（和面板的"对手"块一样）
STRONGEST = "最强"
LABEL_SEP = "·"
GOLDEN_MARK = "(金)"  # 卡名末尾的金色标记：卡图窗口靠金色外圈表示，名字里不写
SLOTS = 7  # 每行最多几个卡图（场上最多 7 个随从）
MAX_OPPONENTS = 7  # 最多几个对手（8 人一局）
MAX_MARKS = 3
# 卡图顶上的小字徽章，按优先级；超级风怒也写"风"；潜行等其他关键词不画
MARKS = (
    ("TAUNT", "嘲"),
    ("POISONOUS", "剧"),
    ("VENOMOUS", "毒"),
    ("REBORN", "复"),
    ("WINDFURY", "风"),
    ("MEGA_WINDFURY", "风"),
)

BOARDS_WIDTH = 0.355  # 窗口宽 = 客户区高 × 0.355（16:9 时 = 0.20 W）
BOARDS_RIGHT_LIMIT = 0.22  # 但右边缘不超过客户区宽的 22%（商店第 1 个槽位左边）
BOARDS_TOP = 0.12  # 顶边在客户区高的 12%（让开左上角的名牌）
BOARDS_MAX_HEIGHT = 0.78  # 最高 78% H（到 0.90 H，让开左下角的名牌）
PORTRAIT_GAP = 0.1  # 卡图间距 = 卡图宽 × 0.1
PORTRAIT_ASPECT = 1.25  # 椭圆高 = 宽 × 1.25
PORTRAIT_TOP = 0.25  # 标题行和卡图之间空 卡图宽 × 0.25
BADGE_RADIUS, BADGE_MIN = 0.2, 9  # 攻 / 血徽章半径 = 宽 × 0.2，至少 9 像素
MARK_RADIUS, MARK_MIN = 0.15, 8  # 小字徽章
BADGE_FONT = 1.2  # 徽章字号 = 半径 × 1.2
# 有卡图的行：标题行之下再占 卡图宽 × 1.75（0.25 空隙 + 1.25 椭圆 + 0.12 徽章探出 + 0.13 行距）
ROW_EXTRA = 1.75
ROW_PLAIN = 0.25  # 没有卡图的行：标题行之下只空 0.25
D_MIN = 28  # 卡图最窄 28 像素
SMALL_SCALE = 0.85  # 标题行、已淘汰、附注的字号
TITLE_GAP = 0.4  # 标题下面空几个字高
LINE_SPACING = 1.4  # 一行文字的高度 = 字号 × 1.4（微软雅黑、苹方都在 1.3~1.4）
NAME_SCALE, NAME_MIN = 0.2, 10  # 占位椭圆里的名字字号 = 卡图宽 × 0.2，至少 10 像素
PREVIEW_GAP = 0.5  # Mac 预览：窗口和箱子之间空半个字


@dataclass(frozen=True)
class Portrait:
    card_id: str
    name: str  # 卡名（去掉末尾的"(金)"，金色靠外圈表示）
    atk: int
    health: int
    golden: bool  # is_golden(card_id, golden)
    shield: bool  # DIVINE_SHIELD
    marks: tuple[str, ...]  # 小字徽章，最多 3 个：嘲 / 剧 / 毒 / 复 / 风


@dataclass(frozen=True)
class BoardRow:
    player_id: int
    label: str  # "下个对手" / "对手"（战斗中）/ "最强" / "下个对手·最强" / "对手·最强" / ""
    # opponent_text(..., place=True, seen=True, threat=False, stats=False)：短写，一定带着
    # "第 N 回合见"（窗口画不下时只截它）
    text: str
    tone: Tone  # 有 label 的行 "text"，其余 "dim"
    portraits: tuple[Portrait, ...]  # 最多 7 个，按 position 从左到右；没见过 / 空场时为空
    stats: str = ""  # 短写省掉的阵容大小（"畸变×5 身材 95"、残影"4 个 · 身材 29"）；放得下才画
    hero: str = ""  # text 里的英雄名：短写还放不下时先把它缩成"前几个字…"


@dataclass(frozen=True)
class OpponentBoards:
    title: str  # "对手阵容（上次交手时）· 第 10 回合"
    rows: tuple[BoardRow, ...]
    eliminated: str  # "已淘汰：甲、乙"；没有时空
    note: str  # "日志里只有交手时的阵容，对手之后可能变强了"
    game: int  # 第几局（GAME_STARTED 累加）：换局时窗口自动关上
    version: int = 0  # 内容变了就加一（OverlayModel.boards 给）

    def card_ids(self) -> list[str]:
        """所有卡图的卡牌 ID（去重，按出现的顺序）：要下载的就是这些。"""
        return list(dict.fromkeys(p.card_id for row in self.rows for p in row.portraits))


def _marks(keywords: tuple[str, ...]) -> tuple[str, ...]:
    marks: list[str] = []
    for keyword, mark in MARKS:
        if keyword in keywords and mark not in marks:
            marks.append(mark)
    return tuple(marks[:MAX_MARKS])


def _portrait(m: Minion, name: Namer) -> Portrait:
    return Portrait(
        card_id=m.card_id,
        name=name(m.card_id).removesuffix(GOLDEN_MARK),
        atk=m.atk,
        health=m.health,
        golden=is_golden(m.card_id, m.golden),
        shield="DIVINE_SHIELD" in m.keywords,
        marks=_marks(m.keywords),
    )


def _label(next_: bool, strongest: bool, fighting: bool) -> str:
    parts = []
    if next_:
        parts.append(FIGHTING if fighting else NEXT)
    if strongest:
        parts.append(STRONGEST)
    return LABEL_SEP.join(parts)


def build_boards(
    state: GameState | None, overview: OpponentOverview | None, name: Namer, game: int
) -> OpponentBoards:
    """overview 为 None（面板关掉了对手、整理出错）时这里再算一次：卡图窗口不受 [overlay]
    opponents 影响。没有对局（WAITING、选英雄、还没有对手的数据）时没有行。"""
    if state is None or state.phase in (Phase.WAITING, Phase.HERO_SELECT) or not state.opponents:
        return OpponentBoards(title=NO_GAME, rows=(), eliminated="", note="", game=game)
    if overview is None:
        overview = summarize_opponents(state)
    fighting = state.phase == Phase.COMBAT
    rows = []
    for s in overview.opponents:
        if not s.alive and not s.next:
            continue  # 已淘汰的合成最后一行（残影除外）
        opp = state.opponents.get(s.player_id)
        board = sorted(opp.last_board, key=lambda m: m.position) if opp is not None else []
        label = _label(s.next, s.strongest, fighting)
        rows.append(
            BoardRow(
                player_id=s.player_id,
                label=label,
                text=opponent_text(
                    s, name, overview.turn, place=True, seen=True, threat=False, stats=False
                ),
                tone="text" if label else "dim",
                portraits=tuple(_portrait(m, name) for m in board[:SLOTS]),
                stats=stats_text(s),
                hero=name(s.hero_card_id) if s.hero_card_id else "未知",
            )
        )
    gone = eliminated_names(overview, name)
    return OpponentBoards(
        title=TITLE.format(turn=state.turn),
        rows=tuple(rows),
        eliminated=ELIMINATED + "、".join(gone) if gone else "",
        note=NOTE,
        game=game,
    )


# ---- 排版 ----


@dataclass(frozen=True)
class RowGeometry:
    index: int  # OpponentBoards.rows 的下标
    y: int  # 标题行的顶边（窗口坐标）
    portrait_y: int | None  # 卡图（椭圆）的顶边；这一行没有卡图时 None


@dataclass(frozen=True)
class BoardsLayout:
    width: int
    height: int
    pad: int
    font_px: int
    title_px: int  # 标题：面板字号，粗体
    small_px: int  # 标题行、已淘汰、附注
    title_h: int  # 标题的行高
    small_h: int  # 小字的行高
    d: int  # 卡图宽
    oval_h: int  # 卡图高 = d × 1.25
    gap: int  # 卡图间距
    slots: tuple[int, ...]  # 7 个槽位的 x（窗口坐标）
    badge_r: int
    badge_px: int
    mark_r: int
    mark_px: int
    name_px: int  # 占位椭圆里的名字
    rows: tuple[RowGeometry, ...]  # 放得下的行（从第一行起连续）
    overflow: int  # 放不下、去掉了的对手行数；0 时没有"还有 N 个对手放不下"
    overflow_y: int | None
    eliminated_y: int | None
    note_y: int | None


def _line(px: int) -> int:
    return round(px * LINE_SPACING)


def _row_height(small_h: int, d: int, portraits: bool) -> int:
    return small_h + round((ROW_EXTRA if portraits else ROW_PLAIN) * d)


def boards_layout(client: Rect, font_px: int, boards: OpponentBoards) -> BoardsLayout:
    """按客户区（贴不上炉石时是屏幕）和面板字号排版。超高时：先把卡图等比缩小（不小于 D_MIN）；
    仍放不下就去掉附注行，再从最后一行起去掉对手行，末尾补一行"还有 N 个对手放不下"。标题和
    第一行永远保留。"""
    width = min(
        round(BOARDS_WIDTH * client.height),
        round(BOARDS_RIGHT_LIMIT * client.width) - round(SIDE_MARGIN * client.width),
    )
    width = max(width, 1)
    pad = round(font_px * PAD_RATIO)
    natural = max(1, math.floor((width - 2 * pad) / (SLOTS + (SLOTS - 1) * PORTRAIT_GAP)))
    max_height = round(BOARDS_MAX_HEIGHT * client.height)
    small_px = round(SMALL_SCALE * font_px)
    title_h, small_h = _line(font_px), _line(small_px)
    top = pad + title_h + round(TITLE_GAP * font_px)
    has = [bool(row.portraits) for row in boards.rows]
    count = len(has)

    def height(d: int, rows: int, note: bool) -> int:
        lines = (rows < count) + bool(boards.eliminated) + note
        body = sum(_row_height(small_h, d, has[i]) for i in range(rows))
        return top + body + lines * small_h + pad

    d, note = natural, bool(boards.note)
    while d > min(natural, D_MIN) and height(d, count, note) > max_height:
        d -= 1
    if note and height(d, count, note) > max_height:
        note = False
    rows = count
    while rows > 1 and height(d, rows, note) > max_height:
        rows -= 1

    y = top
    geometry = []
    for i in range(rows):
        portrait_y = y + small_h + round(PORTRAIT_TOP * d) if has[i] else None
        geometry.append(RowGeometry(i, y, portrait_y))
        y += _row_height(small_h, d, has[i])
    lines: dict[str, int | None] = {}
    for key, shown in (
        ("overflow", rows < count),
        ("eliminated", bool(boards.eliminated)),
        ("note", note),
    ):
        lines[key] = y if shown else None
        y += small_h if shown else 0
    gap = int(d * PORTRAIT_GAP)
    badge_r = max(BADGE_MIN, round(BADGE_RADIUS * d))
    mark_r = max(MARK_MIN, round(MARK_RADIUS * d))
    return BoardsLayout(
        width=width,
        height=y + pad,
        pad=pad,
        font_px=font_px,
        title_px=font_px,
        small_px=small_px,
        title_h=title_h,
        small_h=small_h,
        d=d,
        oval_h=round(PORTRAIT_ASPECT * d),
        gap=gap,
        slots=tuple(pad + i * (d + gap) for i in range(SLOTS)),
        badge_r=badge_r,
        badge_px=round(BADGE_FONT * badge_r),
        mark_r=mark_r,
        mark_px=round(BADGE_FONT * mark_r),
        name_px=max(NAME_MIN, round(NAME_SCALE * d)),
        rows=tuple(geometry),
        overflow=count - rows,
        overflow_y=lines["overflow"],
        eliminated_y=lines["eliminated"],
        note_y=lines["note"],
    )


# ---- 位置 ----


def boards_side(area: Rect, box: Rect) -> Side:
    """卡图窗口放哪边：面板（连同标签的箱子）的中心在客户区左半边时放右边，否则放左边（顶部
    预设的箱子中心正好在中间，算右半边 → 窗口在左边）。"""
    return "right" if 2 * box.x + box.width < 2 * area.x + area.width else "left"


def boards_rect(
    area: Rect,
    screen: Rect,
    box: Rect,
    layout: BoardsLayout,
    docked: bool,
    side: Side | None = None,
) -> Rect:
    """贴靠炉石时：顶边在客户区高的 12%，默认离左边 2%，side（None 时按 boards_side）是右边时
    离右边 2%；再夹进客户区和屏幕。Mac 预览和退路（区域就是屏幕）：放在箱子左边、顶边对齐，
    屏幕太窄放不下时贴屏幕左边。"""
    width, height = layout.width, layout.height
    if not docked:
        x = box.x - round(PREVIEW_GAP * layout.font_px) - width
        x, y = keep_inside(x, box.y, width, height, screen, screen)
        return Rect(x, y, width, height)
    margin = round(SIDE_MARGIN * area.width)
    if (side or boards_side(area, box)) == "left":
        x = area.x + margin
    else:
        x = area.right - margin - width
    y = area.y + round(BOARDS_TOP * area.height)
    x, y = keep_inside(x, y, width, height, area, screen)
    return Rect(x, y, width, height)
