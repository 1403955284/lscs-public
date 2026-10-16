"""对手阵容卡图窗口（tkinter）：点"对手"标签后贴在炉石客户区左侧（面板在左边时在右侧），每个对手
一行——标题行和最多 7 个椭圆卡图，卡图下角攻击 / 生命徽章，金色外圈、圣盾外圈、顶上的小字徽章
（对手阵容卡图窗口设计文档第 5 节）。内容和排版由 overlay.boards 算好，这里只照着画。

它是面板 Tk 的又一个 Toplevel，里面一整块 Canvas：
- Windows：分层（和面板同一个不透明度）、不进任务栏、不抢焦点，而且**整块鼠标穿透**（点它盖住的
  排行榜头像照样点到游戏）；每次定位时检查样式还在不在（apply_style）；
- 非 Windows（replay --overlay 预览）：普通置顶窗口（-alpha），不穿透。Windows 上退回普通置顶
  窗口时不用它（没法穿透会挡住游戏），OverlayWindow 直接关掉它和"对手"标签；
- 隐藏用透明度 0（关着时还移到屏幕外），从不 withdraw / deiconify / focus_set。

卡图照片由 overlay.portraits.Portraits 持有引用（画布项不算引用）；销毁窗口前先清掉它。
只在主线程使用。出错时抛出异常，由 OverlayWindow 决定怎么处理（关掉标签和这个窗口，面板照常）。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections import Counter
from collections.abc import Callable

from lscs.overlay import geometry, win32
from lscs.overlay.boards import OVERFLOW, STATS_SEP, BoardsLayout, OpponentBoards, Portrait
from lscs.overlay.geometry import Rect
from lscs.overlay.portraits import Portraits
from lscs.overlay.style import BADGE_FILL, BADGE_RING
from lscs.overlay.theme import (
    BG,
    DIM,
    HEALTH_RING,
    MUTED,
    NOW_BG,
    OFFSCREEN,
    OPP_TAG,
    POISON_MARK,
    PORTRAIT_RING,
    REBORN_MARK,
    RULES_TAG,
    SHIELD_RING,
    TEXT,
    TITLE,
    WINDFURY_MARK,
)

Fonts = Callable[[int, bool], tkfont.Font]  # (像素, 粗体) → 字体（面板的字体缓存）
MARK_COLORS = {"嘲": TITLE, "剧": POISON_MARK, "毒": POISON_MARK, "复": REBORN_MARK}
MARK_SPACING = 2.2  # 小字徽章的间距 = 半径 × 2.2
MARK_DROP = 0.4  # 小字徽章的圆心在椭圆顶边下面 半径 × 0.4
BADGE_X = (0.22, 0.78)  # 攻击、生命徽章的圆心 x = 卡图左边 + d × 0.22 / 0.78
BADGE_UP = 0.08  # 圆心 y = 椭圆底边 − d × 0.08
BIG_NUMBER = 100  # 三位数的徽章字缩到 0.8 倍
BIG_SCALE = 0.8
GOLDEN_RING, PLAIN_RING = 3, 2  # 椭圆外圈的宽度
SHIELD_WIDTH, SHIELD_GAP = 2, 2  # 圣盾外圈的宽度、离外圈多远
LABEL_SPACE = 0.4  # 行首标签和正文之间空几个字
NAME_CHARS = 2  # 占位椭圆里的名字：两行各 2 个字
TONES = {"text": TEXT, "dim": DIM, "muted": MUTED}


def _one_line(text: str, width: int, font: tkfont.Font) -> str:
    """一行放不下时截掉末尾加"…"。"""
    width = max(width, 1)
    if font.measure(text) <= width:
        return text
    lines = geometry.wrap(text, width, font.measure, names=False)
    return geometry.clip(lines, 1, width, font.measure)[0]


def _header(text: str, stats: str, hero: str, width: int, font: tkfont.Font) -> str:
    """标题行，一定留着"第 N 回合见"：放得下就在短写后面加上阵容的大小；放不下先省掉它；短写
    还放不下就把英雄名缩成"前几个字…"；英雄名缩到一个字还放不下（窗口极小）才截掉末尾。"""
    if stats:
        full = f"{text}{STATS_SEP}{stats}"
        if font.measure(full) <= width:
            return full
    if font.measure(text) <= width:
        return text
    if hero and hero in text:
        for keep in range(len(hero) - 1, 0, -1):
            shorter = text.replace(hero, hero[:keep] + "…", 1)
            if font.measure(shorter) <= width:
                return shorter
    return _one_line(text, width, font)


def _short_name(name: str) -> str:
    head, tail = name[:NAME_CHARS], name[NAME_CHARS : 2 * NAME_CHARS]
    return f"{head}\n{tail}" if tail else head


class BoardsWindow:
    def __init__(
        self,
        root: tk.Misc,
        opacity: float,
        native: bool,
        fonts: Fonts,
        portraits: Portraits,
    ) -> None:
        """native：Windows 上贴靠炉石（加穿透、不抢焦点的样式）；否则是普通置顶窗口。
        创建后放在屏幕外、完全透明（关着）。"""
        self._opacity = opacity
        self._native = native
        self._fonts = fonts
        self._portraits = portraits
        self._hwnd: int | None = None
        self._shown = False
        self._rect: Rect | None = None
        self._drawn: tuple | None = None  # 上次画的 (内容, 排版, 卡图的 generation)
        self._top = tk.Toplevel(root)
        try:
            self._setup()
        except BaseException:
            self.destroy()
            raise

    def _setup(self) -> None:
        top = self._top
        top.title("lscs 对手阵容")
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        top.configure(bg=BG)
        top.geometry(OFFSCREEN)
        self._canvas = tk.Canvas(top, bg=BG, highlightthickness=0, borderwidth=0)
        self._canvas.pack(fill="both", expand=True)
        top.update()  # 让 Tk 创建顶层窗口，才能拿到句柄
        if self._native:
            self.apply_style()
        else:
            top.attributes("-alpha", 0.0)

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    def apply_style(self) -> bool:
        """Windows：加上（或重新加上）分层、不进任务栏、不抢焦点、穿透的样式；Tk 重建了顶层窗口
        时也能恢复。返回 True 表示句柄变了（重建过，不算第一次）：调用方要把面板和标签放回它
        上面。普通置顶窗口什么都不做。"""
        if not self._native:
            return False
        hwnd = win32.root_window(self._top.winfo_id())
        if hwnd is None:
            raise RuntimeError("拿不到对手阵容窗口的句柄（GetAncestor 返回空）")
        if hwnd == self._hwnd and win32.has_overlay_style(hwnd, True):
            return False
        if not win32.make_overlay(hwnd, self._opacity, True):  # 失败时抛出 Win32Error
            raise RuntimeError("不是 Windows")
        rebuilt = self._hwnd is not None and hwnd != self._hwnd
        self._hwnd = hwnd
        if not self._shown:  # make_overlay 把透明度设回了 opacity：关着就再藏起来
            self._set_opacity(0.0)
        return rebuilt

    def _set_opacity(self, opacity: float) -> None:
        if self._native:
            if self._hwnd is not None and not win32.set_opacity(self._hwnd, opacity):
                raise RuntimeError("无法设置对手阵容窗口的透明度")
        else:
            self._top.attributes("-alpha", opacity)

    def _set_shown(self, shown: bool) -> None:
        if shown != self._shown:
            self._set_opacity(self._opacity if shown else 0.0)
            self._shown = shown

    def show(self, boards: OpponentBoards, layout: BoardsLayout, rect: Rect) -> None:
        """放到 rect、画出内容（位置、内容、排版或卡图变了才改），再显示出来。"""
        if rect != self._rect:
            self._top.geometry(f"{rect.width}x{rect.height}+{rect.x}+{rect.y}")
            self._rect = rect
        key = (boards, layout, self._portraits.generation)
        if key != self._drawn:
            self._draw(boards, layout)
            self._drawn = key
        self._set_shown(True)

    def hide(self, offscreen: bool) -> None:
        """透明度 0；offscreen（关掉了）时还移到屏幕外。面板隐藏时位置不动（和面板一样）。
        画布不清：下次打开直接显示，再按新数据重画。"""
        if offscreen and self._rect is not None:
            self._top.geometry(OFFSCREEN)
            self._rect = None
        self._set_shown(False)

    def lift(self) -> None:
        self._top.lift()

    def destroy(self) -> None:
        self._portraits.clear()  # 先放掉照片的引用
        try:
            self._top.destroy()
        except tk.TclError:
            pass

    # ---- 绘制 ----

    def _draw(self, boards: OpponentBoards, layout: BoardsLayout) -> None:
        c = self._canvas
        c.delete("all")
        c.configure(width=layout.width, height=layout.height)
        pad = layout.pad
        inner = layout.width - 2 * pad
        title = self._fonts(layout.title_px, True)
        small = self._fonts(layout.small_px, False)
        small_bold = self._fonts(layout.small_px, True)
        space = max(4, round(layout.small_px * LABEL_SPACE))
        c.create_text(
            pad,
            pad,
            text=_one_line(boards.title, inner, title),
            fill=TITLE,
            font=title,
            anchor="nw",
            tags=("title",),
        )
        for row_geometry in layout.rows:
            row = boards.rows[row_geometry.index]
            x = pad
            if row.label:
                c.create_text(
                    x,
                    row_geometry.y,
                    text=row.label,
                    fill=OPP_TAG,
                    font=small_bold,
                    anchor="nw",
                    tags=("label",),
                )
                x += small_bold.measure(row.label) + space
            text = _header(row.text, row.stats, row.hero, pad + inner - x, small)
            c.create_text(
                x,
                row_geometry.y,
                text=text,
                fill=TONES[row.tone],
                font=small,
                anchor="nw",
                tags=("row",),
            )
            if row_geometry.portrait_y is not None:
                for slot, portrait in zip(layout.slots, row.portraits, strict=False):
                    self._portrait(portrait, slot, row_geometry.portrait_y, layout)
        lines = (
            (layout.overflow_y, OVERFLOW.format(count=layout.overflow)),
            (layout.eliminated_y, boards.eliminated),
            (layout.note_y, boards.note),
        )
        for y, text in lines:
            if y is not None:
                c.create_text(
                    pad,
                    y,
                    text=_one_line(text, inner, small),
                    fill=MUTED,
                    font=small,
                    anchor="nw",
                    tags=("note",),
                )

    def _portrait(self, p: Portrait, x: int, y: int, layout: BoardsLayout) -> None:
        """一个卡图：照片（没有时深色椭圆 + 名字）、外圈、圣盾、顶上的小字徽章、攻 / 血徽章。"""
        c = self._canvas
        d, h = layout.d, layout.oval_h
        photo = self._portraits.photo(p.card_id, d)
        if photo is not None:
            c.create_image(x, y, image=photo, anchor="nw", tags=("art",))
        else:
            c.create_oval(x, y, x + d, y + h, fill=NOW_BG, outline="", tags=("placeholder",))
            name_font = self._fonts(layout.name_px, False)
            c.create_text(
                x + d / 2,
                y + h / 2,
                text=_short_name(p.name),
                fill=MUTED,
                font=name_font,
                justify="center",
                tags=("name",),
            )
        ring = GOLDEN_RING if p.golden else PLAIN_RING
        color = RULES_TAG if p.golden else PORTRAIT_RING
        c.create_oval(x, y, x + d, y + h, outline=color, width=ring, tags=("ring",))
        if p.shield:
            out = ring // 2 + SHIELD_GAP + SHIELD_WIDTH // 2
            c.create_oval(
                x - out,
                y - out,
                x + d + out,
                y + h + out,
                outline=SHIELD_RING,
                width=SHIELD_WIDTH,
                tags=("shield",),
            )
        r = layout.mark_r
        mark_font = self._fonts(layout.mark_px, True)
        centre = x + d / 2
        for i, mark in enumerate(p.marks):
            cx = centre + (i - (len(p.marks) - 1) / 2) * MARK_SPACING * r
            cy = y + MARK_DROP * r
            self._badge(cx, cy, r, mark, MARK_COLORS.get(mark, WINDFURY_MARK), mark_font, "mark")
        r = layout.badge_r
        cy = y + h - BADGE_UP * d
        numbers = ((BADGE_X[0], p.atk, RULES_TAG), (BADGE_X[1], p.health, HEALTH_RING))
        for share, value, ring_color in numbers:
            px = layout.badge_px if value < BIG_NUMBER else round(layout.badge_px * BIG_SCALE)
            font = self._fonts(px, True)
            self._badge(x + share * d, cy, r, str(value), ring_color, font, "badge", TEXT)

    def _badge(
        self,
        cx: float,
        cy: float,
        r: int,
        text: str,
        ring: str,
        font: tkfont.Font,
        tag: str,
        text_color: str | None = None,
    ) -> None:
        """深色小圆 + 彩色外圈，中间一个字或一个数（和画面高亮的"核"字徽章同一种画法）。"""
        c = self._canvas
        c.create_oval(
            cx - r,
            cy - r,
            cx + r,
            cy + r,
            fill=BADGE_FILL,
            outline=ring,
            width=BADGE_RING,
            tags=(tag,),
        )
        c.create_text(cx, cy, text=text, fill=text_color or ring, font=font, tags=(tag + "_text",))

    # ---- 冒烟测试用 ----

    def texts(self) -> list[str]:
        """画布上所有文字，按画的顺序（标题、每行的标签和正文、卡图的字、底部的灰字）。"""
        c = self._canvas
        return [c.itemcget(i, "text") for i in c.find_all() if c.type(i) == "text"]

    def shapes(self) -> dict[str, int]:
        """画布上的椭圆按用途数：ring 卡图外圈、shield 圣盾、placeholder 占位、badge 攻 / 血、
        mark 小字徽章。"""
        c = self._canvas
        return dict(Counter(c.gettags(i)[0] for i in c.find_all() if c.type(i) == "oval"))

    def images(self) -> int:
        """画布上 image 项（卡图照片）的个数。"""
        c = self._canvas
        return sum(1 for i in c.find_all() if c.type(i) == "image")

    @property
    def geometry(self) -> Rect | None:
        return self._rect

    @property
    def canvas(self) -> tk.Canvas:
        return self._canvas

    @property
    def shown(self) -> bool:
        return self._shown
