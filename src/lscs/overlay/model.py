"""悬浮面板的数据模型（面板精简与详细模式设计文档）。

监听线程调用 on_update 和建议方法（与 AdviceDisplay 相同），界面线程定时调用 snapshot()；
所有读写都在锁里。内容变化时版本号加一，界面只在版本号变化时重画。

终端是日志，面板是"现在该做什么"：规则和 AI 的方案连同出建议时的局面一起保存，每次状态更新
都去掉已经做完的步骤，再按当前局面重新排版。面板只显示一套方案——"下一步"来自的那套（本回合
的深度分析 → 快速建议 → 规则，剩余的第一步要做得起），页眉右侧小字标来源；另一套方案、完整的
理由、要找和对子的张数估计都只在终端里。

视图（OverlayView）从上到下：页眉（回合 · 铸币 · 本，右侧来源）、"下一步"（一句大字、一行
小圆点，选项、选英雄和站位再加一行理由；带画面高亮要框的位置 Target）、若干块（Block）、
底部一行提示（notice）和警告。精简模式只有一个没有标签的块，每行前面一个粗体标签：

- 招募："然后"（剩下的步骤，短写）、"目标"（AI 的一句话；没有 AI 时规则的"方向"）、"计划"
  （整局计划：目标流派 · 阶段 · 核心卡，✓ 是已经有的；还没有计划时没有这一行）；
- 站位（方案做完后"下一步"是站位的第一步）：理由的标签是"为何"；
- 弹出选项："理由"、选项标题 + 按顺序的选项名（★ AI 推荐，▲ 本版本战绩最好的饰品）；
- 选英雄："理由"、"护甲"或"名次"（★ 是"下一步"指的那个）；
- 战斗中：标题带正在打的对手，下面至多一行"最强"；
- 对局结束：复盘的关键时刻和下局重点。

详细模式（[overlay] detail = true）的"下一步"在一个带底色的"现在"块里，下面分块，块标签写
用途：本回合（剩下的每一步、站位、目标、整局计划、"计划提醒"、"规则另议"）、留意（要找和
对子、下个对手和最强）、
选择（每个选项一行，右列本版本统计）、备选和点评（选英雄）、对手（战斗中所有对手）、复盘。

面板太高时 condense 逐级去掉次要的行（界面从第 0 级试到最后一级）。

战斗结果的横幅（overlay.banner）：面板不显示（战斗中的面板也不写结果），只放进快照的 banner
给画面高亮窗口画在棋盘顶部。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from lscs.advisor.context import build_context
from lscs.advisor.display import CIRCLED, describe_action, hero_comment_lines
from lscs.advisor.models import (
    Action,
    Advice,
    ChoiceAdvice,
    GamePlan,
    HeroAdvice,
    Lane,
    Move,
    Plan,
    PositionAdvice,
    StatsNote,
)
from lscs.advisor.names import armor_zh, choice_title, tribe_zh
from lscs.advisor.opponents import (
    OpponentOverview,
    OpponentSummary,
    eliminated_names,
    summarize_opponents,
)
from lscs.advisor.plan_check import key_cards_text, off_plan_note
from lscs.advisor.pool import PoolNote, note_for
from lscs.advisor.positioning import where_text
from lscs.advisor.runner import remaining_plan
from lscs.llm.notices import AUTH_DISABLED, BUDGET_EXHAUSTED
from lscs.llm.parse import MAX_ACTIONS  # 详细模式最多列几步：和 AI 回答的上限一致
from lscs.overlay.banner import Banner, BannerTracker
from lscs.overlay.geometry import NAME_SEP, STEP_SEP
from lscs.review.report import CIRCLED as REVIEW_NUMBERS
from lscs.review.report import GameReview, saved_text
from lscs.state.build import HERO_PLACEHOLDER
from lscs.state.models import Choice, Event, EventType, GameState, HeroSelect, Phase

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
BuyCost = Callable[[str | None], int]  # 英雄卡牌 ID → 买一张随从要几金（rules.buy_cost_for）
Tone = Literal["text", "dim", "muted"]  # 正文 / 次要（比正文暗一档）/ 提示和状态（灰）
# 行的用途，面板太高时按它精简（见 condense）：main 一般的行；step 方案的一步；goal 目标、
# 方向、下局重点（精简到后面时只留一行）；alt 规则另议 / 另选；aside 精简模式的次要行（然后、
# 选项名、护甲 / 名次）；note 附注；comment 点评；opponent 战斗中的其他对手；stats 选项、
# 英雄的条目（右列写统计）
Role = Literal["main", "step", "goal", "alt", "aside", "note", "comment", "opponent", "stats"]
BlockKind = Literal["lines", "plan", "aside", "choice", "heroes", "comments", "opponents", "review"]
Row = Literal["shop", "board", "choice", "hero"]  # 画面上的一排牌
PlanSource = Literal["deep", "fast", "rules"]  # "下一步"来自哪一路的方案
Button = Literal["upgrade", "refresh", "freeze", "hero_power"]  # 画面上的按钮

IDLE = "lscs 已就绪，等待招募阶段"
UNCERTAIN = "⚠ 状态可能不准"
RULES_ERROR = "本回合规则建议出错"
MAX_REASON = 40  # "目标"（没有目标时的分析）最多几个字，超出加"…"
# "要找 ……"最多几个字，按整张牌截断、后面加"…"（界面按像素折行，这里只是防止 AI 列十张牌）
MAX_LOOK_FOR = 40
DONE = "已按建议操作完"  # 详细模式"本回合"的状态
REFRESHED = "商店已刷新"
STATS_SOURCE = "Firestone"  # 本版本统计的署名（短写）
HERO_TITLE = "选英雄 · 护甲越少越强"
HERO_STATS_TITLE = "选英雄 · 本版本战绩 · Firestone"  # 有本版本统计时
PENDING = "分析中…"
REPAIRING = "改正中…"  # AI 的回答不合法，正在让它改正（还在等，和"分析中…"一样）
# 页眉右侧的来源
SOURCE_DEEP = "AI 深度"
SOURCE_AI = "AI"
SOURCE_RULES = "规则"
NOTE_SEP = "· "  # 来源后面的灰字（"规则 · 分析中…"）
# 调度器关闭 AI 的提示 → 之后回合常驻的一行（以及是否在新的一局清除）
AI_OFF = {
    AUTH_DISABLED: ("AI 已关闭（Key 无效）", False),
    BUDGET_EXHAUSTED: ("本局 AI 次数已用完", True),
}
# "下一步"
POINT = "▶ "
WAIT_CHOICE = POINT + "等 AI 选…"
SELF_CHOICE = POINT + "自己选一个（AI 没有建议）"
WAIT = POINT + "等建议…"
WAIT_NEW = POINT + "等新的建议…"
ALL_DONE = "✓ 建议的步骤都做完了"
NOTHING_TO_DO = "无需操作"
CHOICE_BADGE = "选"  # 选项、选英雄的高亮框上写的字（方案的步骤写第几步）
BADGE_MOVE = "拖"  # 站位：高亮框住要拖的随从
ROW_LABELS: dict[str, str] = {"shop": "商店", "board": "场面", "choice": "选项", "hero": "英雄"}
GOOD_CHARS = 12  # "做得好："后面只写事实的第一句，最多这么多个字
DEFAULT_BUY_COST = 3  # 买一张随从的默认价格（规则配置里按英雄覆盖）
STAR = "★"  # 推荐的选项 / 英雄（"下一步"指的那个）
BEST = "▲"  # 本版本战绩最好的饰品（不是推荐）
# 精简模式行首的标签
THEN = "然后"
GOAL = "目标"
PLAN = "计划"  # 整局计划（整局计划与流派指南设计文档第 8.2 节）
DIRECTION = "方向"
REASON = "理由"
WHY = "为何"
ARMOR = "护甲"
PLACE = "名次"
STRONGEST = "最强"
# 详细模式的块标签
THIS_TURN = "本回合"
WATCH = "留意"
CHOOSE = "选择"
CANDIDATES = "备选"
COMMENTS = "点评"
OPPONENTS = "对手"
REVIEW = "复盘"
NEXT_IS_STRONGEST = "下个对手也是最强"
ASIDE_LINES = 2  # 然后、选项名、护甲 / 名次、留意的每一行最多折成几行
GOAL_LINES = 2  # 目标、下局重点
REASON_LINES = 2  # "下一步"的理由


@dataclass(frozen=True)
class Line:
    text: str
    tone: Tone = "text"
    role: Role = "main"
    # 精简模式：行首的粗体标签（然后 / 目标 / 方向 / 护甲 / 名次 / 选项标题 / 最强）
    label: str = ""
    right: str = ""  # 详细模式：右对齐的灰小字（饰品统计、英雄护甲或战绩）
    max_lines: int = 0  # 按像素折行后最多几行，超出的末尾加"…"；0 表示不限
    small: bool = False  # 0.85 倍字号（详细模式战斗中的其他对手）


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    lines: tuple[Line, ...]
    label: str = ""  # 详细模式的块标签；精简模式只有一个 kind="lines" 的块，没有标签
    note: str = ""  # 详细模式：标签旁的灰小字（"大型饰品 · 4 选 1 · Firestone"）
    small: bool = False  # 整块 0.85 倍字号


@dataclass(frozen=True)
class Target:
    """画面高亮框住的地方：一排里的一个槽位，或者一个按钮。"""

    row: Row | None = None  # 哪一排
    index: int = 0  # 从左数第几个，0 起
    count: int = 0  # 这一排现在有几个（居中排版要用）
    button: Button | None = None


@dataclass(frozen=True)
class NextStep:
    """面板最上面的"下一步"：一句大字，一行小圆点标出位置，选项、选英雄和站位再加一行理由。"""

    text: str  # "▶ 买 左数第 2 个 飘荡祭品"
    target: Target | None = None  # 高亮画哪里；None 只显示文字
    slots: str = ""  # "商店 ○ ● ○ ○ ○"：没有目标排时为空
    reason: str = ""  # 选项、选英雄、站位的理由；方案的步骤不带理由（终端里有）
    tone: Tone = "text"  # muted：等待、做完了之类的状态
    badge: str = ""  # 高亮框上的编号：方案里的第几步，选项和选英雄写"选"
    reason_label: str = REASON  # 精简模式理由前面的标签；站位那一步是"为何"


@dataclass(frozen=True)
class OverlayView:
    title: str
    compact: bool = True  # 只有标题行（战斗中、没有对局）
    next_step: NextStep | None = None  # 有"下一步"时标题是灰色小字的页眉
    source: str = ""  # 页眉右侧："AI 深度" / "AI" / "规则" / ""
    source_note: str = ""  # 来源后的灰字："· 分析中…" / "· 改正中…" / "分析中…"
    blocks: tuple[Block, ...] = ()
    notice: str | None = None  # 底部灰字：AI 已关闭、AI 的错误、本回合规则建议出错
    warning: str | None = None
    version: int = 0
    banner: Banner | None = None  # 棋盘顶部的战斗结果：画面高亮窗口画，面板不画


def slots_text(row: Row, index: int, count: int) -> str:
    dots = " ".join("●" if i == index else "○" for i in range(count))
    return f"{ROW_LABELS[row]} {dots}"


def reason_lines(level: int, detail: bool = False) -> int:
    """算"下一步"的理由在第 level 级精简时最多几行：精简模式第 2 级起、详细模式第 3 级起一行。"""
    return 1 if level >= (3 if detail else 2) else REASON_LINES


def condense(view: OverlayView, level: int, detail: bool = False) -> OverlayView:
    """面板超高时逐级精简（第 0 级原样）。
    精简模式：1 去掉次要的行（然后、选项名、护甲 / 名次）；2 目标、下局重点只留一行；3 同 2。
    详细模式：1 去掉"留意"块和"点评"块；2 再去掉"规则另议 / 规则另选"、右列的统计、战斗中
    除了正在打的和最强之外的对手；3 目标只留一行。"下一步"的理由见 reason_lines。
    标题、"下一步"、本回合的步骤、选项和英雄的名字、提示和警告总是保留；再超高由界面截掉底部。"""
    if level <= 0:
        return view
    one_line_goal = 3 if detail else 2  # 从这一级起目标只留一行
    blocks = []
    for block in view.blocks:
        if detail and block.kind in ("aside", "comments"):
            continue
        lines = []
        for line in block.lines:
            if not detail and line.role == "aside":
                continue
            if detail and level >= 2:
                if line.role in ("alt", "opponent"):
                    continue
                line = replace(line, right="")
            if level >= one_line_goal and line.role == "goal":
                line = replace(line, max_lines=1)
            lines.append(line)
        if lines:
            blocks.append(replace(block, lines=tuple(lines)))
    return replace(view, blocks=tuple(blocks))


@dataclass(frozen=True)
class _PlanEntry:
    """一条方案建议，连同出建议时的局面。"""

    plan: Plan
    base: GameState
    expired: bool = False  # 深度分析的操作已经不能用，只看思路
    notes: tuple[PoolNote, ...] = ()  # 对子和要找的牌还剩约几张（Advice.pool_notes）
    plan_note: str | None = None  # 方案和整局计划矛盾时的提醒（Advice.plan_note）


@dataclass(frozen=True)
class _HeroEntry:
    """AI 的选英雄建议。"""

    advice: HeroAdvice


@dataclass(frozen=True)
class _ChoiceEntry:
    """AI 的选项建议。"""

    advice: ChoiceAdvice


@dataclass(frozen=True)
class _Recruit:
    """招募时的"下一步"和它来自的方案。"""

    step: NextStep
    entry: _PlanEntry | None  # 显示的方案；None：规则出错或还没来、在等新的 AI 建议
    source: PlanSource
    waiting: bool  # 商店刷新了，在等新的 AI 建议
    plan: Plan | None  # 显示的方案剩下的步骤
    numbers: tuple[int, ...]  # plan 的每一步在完整方案里的序号（0 起）
    moves: tuple[tuple[Move, int], ...]  # 还没做完的站位，和要拖的随从现在在第几个
    on: Literal["plan", "move", "status"]  # "下一步"是方案的第一步、站位的第一步，还是状态
    stale: bool = False  # 方案剩下的步已经做不了（规则的旧方案）：详细模式不再列它们


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit] + "…" if len(text) > limit else text


def _number(index: int) -> str:
    return CIRCLED[index] if index < len(CIRCLED) else str(index + 1)


def _shop_refreshed(base: GameState, state: GameState) -> bool:
    """出建议之后商店换了一批（同一回合、前后都有商品、没有一件相同）。"""
    if state.turn != base.turn or not base.shop or not state.shop:
        return False
    return not {s.entity_id for s in base.shop} & {s.entity_id for s in state.shop}


def _entity_card(state: GameState, entity_id: int | None) -> str | None:
    """场面、商店、手牌里的实体的卡牌 ID。"""
    me = state.me
    cards = [*(me.board if me else ()), *state.shop, *(me.hand if me else ())]
    return next((c.card_id for c in cards if c.entity_id == entity_id), None)


def _move_done(move: Move, index: int, order: list[int]) -> bool:
    """这一步已经做完：它在目标槽位上，而且右边紧挨着的还是那个随从（最左、最右边不看邻居）。"""
    if index != move.index:
        return False
    return move.before_id is None or order[index + 1 : index + 2] == [move.before_id]


def _first_step_unaffordable(plan: Plan, state: GameState, buy_cost: int) -> bool:
    """剩余方案的第一步现在做不起：买（随从按买牌价、酒馆法术按它的 COST）、升本、刷新、
    英雄技能的费用大于铸币。不知道费用（升本 / 刷新费用为 None、法术没有 COST、要买的牌
    不在商店里）时不下结论；卖、冻结不花铸币。"""
    me = state.me
    if me is None or not plan.actions:
        return False
    first = plan.actions[0]
    cost: int | None = None
    match first.type:
        case "buy":
            item = next((s for s in state.shop if s.entity_id == first.entity_id), None)
            if item is not None:
                cost = buy_cost if item.card_type == "MINION" else item.cost
        case "upgrade":
            cost = me.upgrade_cost
        case "refresh":
            cost = me.refresh_cost
        case "hero_power":
            cost = me.hero_power.cost if me.hero_power is not None else None
    return cost is not None and me.gold < cost


def _choosing(state: GameState | None) -> bool:
    """正在选英雄：手牌区里有备选英雄。"""
    return state is not None and state.phase == Phase.HERO_SELECT and state.hero_select is not None


def _waiting_text(status: object) -> str:
    """一路 AI 的状态是在等（"分析中…""改正中…"）时返回它，否则为空。"""
    return status if isinstance(status, str) and status in (PENDING, REPAIRING) else ""


def _error_text(status: object) -> str | None:
    """一路 AI 收到的错误、提示（不是"分析中…""改正中…"）。"""
    if isinstance(status, str) and status not in (PENDING, REPAIRING):
        return status
    return None


def _with_note(source: str, waiting: str) -> str:
    """来源后面的灰字：有来源时前面加"· "。"""
    if not waiting:
        return ""
    return NOTE_SEP + waiting if source else waiting


def _board_brief(s: OpponentSummary) -> str:
    """对手最近一次见到的阵容，一小段：畸变×5 身材 194 / 身材 30 / 空场 / 还没见过；
    残影写"残影 · 4 个 身材 29"。"""
    if s.seen_turn is None:
        board = "还没见过"
    elif s.empty_board:
        board = "空场"
    elif not s.alive:
        board = f"{s.minions} 个 身材 {s.total_stats}"
    else:
        board = f"身材 {s.total_stats}"
        if s.main_tribe is not None:
            board = f"{tribe_zh(s.main_tribe)}×{s.tribe_count} {board}"
    return board if s.alive else f"残影 · {board}"


def _opponent_text(
    s: OpponentSummary, name: Namer, turn: int, *, place: bool, seen: bool, threat: bool
) -> str:
    """详细模式战斗中一个对手的写法：名字 血量+护甲 · N本 · 主种族×数 身材 S（· 第 T 回合见）
    （· 威胁 N）；淘汰的只写"已淘汰"，残影写它最后的阵容有几个、多大。place：前面加名次。"""
    hero = name(s.hero_card_id) if s.hero_card_id else "未知"
    head = f"第{s.place if s.place is not None else '?'}名 {hero}" if place else hero
    when = "本回合见" if s.seen_turn == turn else f"第 {s.seen_turn} 回合见"
    if not s.alive:
        if not s.next:
            return f"{head} 已淘汰"
        if s.seen_turn is None:
            return f"{head} 已淘汰（残影）· 还没见过阵容"
        if s.empty_board:
            return f"{head} 已淘汰（残影）· {when}到空场"
        return f"{head} 已淘汰（残影）· {when} {s.minions} 个 · 身材 {s.total_stats}"
    tier = "?" if s.tavern_tier is None else s.tavern_tier
    parts = [f"{head} {s.health}+{s.armor}", f"{tier}本"]
    if s.seen_turn is None:
        parts.append("还没见过阵容")
    elif s.empty_board:
        parts.append(f"{when}到空场" if seen else "见到空场")
    else:
        stats = f"身材 {s.total_stats}"
        if s.main_tribe is not None:
            stats = f"{tribe_zh(s.main_tribe)}×{s.tribe_count} {stats}"
        parts.append(stats)
        if seen:
            parts.append(when)
    if threat:
        parts.append(f"威胁 {s.threat}")
    return " · ".join(parts)


def _look_for_text(look_for: tuple[str, ...], notes: tuple[PoolNote, ...]) -> str:
    """写成"要找 A（约 11 张）、B"：按整张牌截断，放得下几张写几张，后面还有就加"…"；一张都
    放不下时按字截。"""
    parts = []
    for text in look_for:
        note = note_for(text, notes)
        parts.append(f"{text}（约 {note.left} 张）" if note is not None else text)
    shown: list[str] = []
    for part in parts:
        if len("要找 " + "、".join([*shown, part])) > MAX_LOOK_FOR:
            break
        shown.append(part)
    if not shown:
        return _clip("要找 " + parts[0], MAX_LOOK_FOR)
    return "要找 " + "、".join(shown) + ("…" if len(shown) < len(parts) else "")


def _pairs_text(notes: tuple[PoolNote, ...]) -> str:
    """写成"对子 蚀脑怪×2 还剩约 11 张"：对子写"×N"，差一张成对子的写"×1 商店有"；没有时为空。"""
    parts = []
    for note in notes:
        if note.have >= 2:
            parts.append(f"{note.name}×{note.have} 还剩约 {note.left} 张")
        elif note.pair:
            parts.append(f"{note.name}×1 商店有 还剩约 {note.left} 张")
    return "对子 " + "；".join(parts) if parts else ""


def _action_key(action: Action) -> tuple[str, int | None]:
    """比较两套方案的步：买、卖、英雄技能看动作和实体，升本、刷新、冻结只看动作。"""
    if action.type in ("buy", "sell", "hero_power"):
        return action.type, action.entity_id
    return action.type, None


def _single_block(lines: list[Line]) -> tuple[Block, ...]:
    """精简模式：所有的行放在一个没有标签的块里。"""
    return (Block("lines", tuple(lines)),) if lines else ()


REVIEW_PENDING = "AI 复盘生成中…"


def review_lines(review: GameReview, detail: bool = True) -> tuple[Line, ...]:
    """复盘：每个关键时刻一行（"① 第 N 回合 升本不补场面 → 连掉 30 血"，做得好的写事实的
    第一句），下局重点，文件路径（灰字）。AI 复盘到了换成 AI 的标题和重点；还在生成时最后一行
    "AI 复盘生成中…"（灰字），失败时去掉这行。detail=False（精简模式）：关键时刻每条一行、
    下局重点最多两行，不写文件路径和"生成中"。"""
    lines = []
    moment_lines = 0 if detail else 1
    ai = review.ai if review.ai_state == "done" else None
    if ai is not None:
        for i, moment in enumerate(ai.moments):
            text = f"{REVIEW_NUMBERS[i]} 第 {moment.turn} 回合 {moment.title}"
            lines.append(Line(text, max_lines=moment_lines))
        focus = ai.focus
    else:
        for i, flag in enumerate(review.moments):
            text = f"{REVIEW_NUMBERS[i]} {flag.title}"
            if flag.kind == "good_turn":
                text += "：" + _clip(flag.facts.split("，", 1)[0], GOOD_CHARS)
            elif flag.streak_lost:
                text += f" → 连掉 {flag.streak_lost} 血"
            lines.append(Line(text, max_lines=moment_lines))
        if not review.moments:
            lines.append(Line("这局没有触发提醒", "muted"))
        focus = review.focus
    if not detail:
        lines.append(Line(f"下局重点：{focus}", role="goal", max_lines=GOAL_LINES))
        return tuple(lines)
    lines.append(Line(f"下局重点：{focus}"))
    lines.append(Line(saved_text(review, updated=ai is not None), "muted", role="comment"))
    if review.ai_state == "pending":
        lines.append(Line(REVIEW_PENDING, "muted"))
    return tuple(lines)


def _recruit_title(state: GameState) -> str:
    parts = [f"第 {state.turn} 回合"]
    me = state.me
    if me is not None:
        parts += [f"铸币 {me.gold}", f"{me.tavern_tier} 本"]
        # 满本时没有升本按钮，所以没有升本费用；招募开始（MAIN_ACTION）之前按钮还没摆出来，
        # 也没有升本费用，这时不写（不能说成"已满本"）
        if state.main_action:
            parts.append("已满本" if me.upgrade_cost is None else f"升本 {me.upgrade_cost}")
    return " · ".join(parts)


class OverlayModel:
    def __init__(
        self,
        name: Namer,
        ai_enabled: bool = True,
        choice_ai: bool = True,
        opponents: bool = True,
        combat_result: bool = True,
        clock: Callable[[], float] | None = None,
        buy_cost: BuyCost = lambda _hero: DEFAULT_BUY_COST,
        detail: bool = False,
    ) -> None:
        """choice_ai：弹出选项时会不会调用 AI（config.toml 的 choice_advice）。
        opponents：写对手（config.toml 的 [overlay] opponents）。
        combat_result、clock：战斗结果的横幅（[combat_result]，见 overlay.banner）。
        buy_cost：按英雄查买一张随从要几金（规则配置的 buy_cost_for），用来判断方案的第一步
        做不做得起。detail：详细模式（[overlay] detail），否则精简模式。"""
        self._name = name
        self._banners = BannerTracker(combat_result, clock)
        self._detail = detail
        self._buy_cost = buy_cost
        self._opponents_enabled = opponents
        self._ai_enabled = ai_enabled
        self._choice_enabled = ai_enabled and choice_ai
        self._lock = threading.Lock()
        self._version = 0
        self._content = OverlayView(title=IDLE)
        self._state: GameState | None = None
        self._turn: int | None = None  # 下面的规则和 AI 内容属于哪个回合
        # 每一路的内容：方案（或推荐），或者一行状态（"分析中…""改正中…"、错误提示）
        self._rules: _PlanEntry | str | None = None  # 出错时是 RULES_ERROR
        self._ai: _PlanEntry | str | None = None  # 快速建议
        self._deep: _PlanEntry | None = None  # 本回合的深度分析
        self._deep_pending = False
        self._hero_rules: HeroAdvice | None = None  # 选英雄的规则建议
        self._hero_ai: _HeroEntry | str | None = None
        self._choice_ai: _ChoiceEntry | str | None = None
        self._ai_off: tuple[str, bool] | None = None  # (常驻提示, 新的一局是否清除)
        self._message: str | None = None
        self._overview_failed = False  # 整理对手一览出过错（只记一次警告）
        self._review: GameReview | None = None  # 对局结束后的复盘
        self._position: PositionAdvice | None = None  # 本回合最近一次站位建议
        self._stats_note: StatsNote | None = None  # 这组备选英雄或饰品的本版本统计
        self._game_plan: GamePlan | None = None  # 整局计划：换回合保留，新对局清空

    # ---- 界面线程 ----

    def snapshot(self) -> OverlayView:
        with self._lock:
            return replace(self._content, version=self._version)

    # ---- 监听线程 ----

    def on_update(self, state: GameState, events: list[Event]) -> None:
        with self._lock:
            self._state = state
            if any(e.type == EventType.GAME_STARTED for e in events):
                if self._ai_off is not None and self._ai_off[1]:
                    self._ai_off = None
                self._hero_rules, self._hero_ai = None, None  # 新对局（或重连）：重新开始
                self._review = None
                self._game_plan = None
            if any(e.type == EventType.HEROES_OFFERED for e in events):
                self._hero_ai = None  # 新的一组备选（重随）：上一组的推荐、错误和提示都不再显示
                self._stats_note = None
            self._banners.on_update(events)
            if state.choice is None or any(e.type == EventType.CHOICE_OFFERED for e in events):
                self._choice_ai = None  # 选完了，或者换了一组
                if self._stats_note is not None and self._stats_note.kind == "trinket":
                    self._stats_note = None
            if state.phase in (Phase.RECRUIT, Phase.COMBAT):
                self._enter_turn(state.turn)
            else:
                self._enter_turn(None)  # 没有对局：下一局即使回合数相同也重新开始
            self._commit()

    def show_message(self, text: str) -> None:
        """覆盖所有内容的一行提示，例如"监听已停止"。"""
        with self._lock:
            self._message = text
            self._commit()

    def show_opponents(self, state: GameState, overview: OpponentOverview) -> None:
        """面板每次 on_update 都自己整理对手一览（战斗中也要更新），这里什么都不做。"""

    def show_rules(self, state: GameState, advice: Advice) -> None:
        with self._lock:
            self._enter_turn(advice.turn)
            self._rules = _PlanEntry(advice.plan, state, notes=advice.pool_notes)
            self._commit()

    def rules_error(self, hero: bool = False) -> None:
        if hero:
            return  # 选英雄时照样列出英雄和护甲，只是没有 ★（出错的提示在终端里）
        with self._lock:
            self._rules = RULES_ERROR
            self._commit()

    def ai_pending(self, lane: Lane = "fast") -> None:
        with self._lock:
            if lane == "deep":
                self._deep, self._deep_pending = None, True
            elif lane == "hero":
                self._hero_ai = PENDING
            elif lane == "choice":
                self._choice_ai = PENDING
            else:
                self._ai = PENDING
            self._commit()

    def ai_text(self, text: str, lane: Lane = "fast") -> None:
        with self._lock:
            if lane == "deep":
                self._deep_pending = False  # 深度分析出错、超时不影响快速建议，只在终端显示
            elif lane == "choice":
                self._choice_ai = text
            else:
                if text in AI_OFF:
                    self._ai_off = AI_OFF[text]
                # 调度器的提示（预算用完、局面出错）不分路：正在选英雄时算选英雄这一路的
                if lane == "hero" or _choosing(self._state):
                    self._hero_ai = text
                else:
                    self._ai = text
            self._commit()

    def ai_repairing(self, lane: Lane, reason: str) -> None:
        """AI 的回答不合法、正在改正：快速、选英雄、选项这一路记成"改正中…"（原因只在终端
        里）。深度分析不改任何状态，面板上还是在等它。"""
        if lane == "deep":
            return
        with self._lock:
            if lane == "hero":
                self._hero_ai = REPAIRING
            elif lane == "choice":
                self._choice_ai = REPAIRING
            else:
                self._ai = REPAIRING
            self._commit()

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        """agrees（AI 同意规则建议）只在终端里说；面板照样按 AI 的步骤列。"""
        entry = _PlanEntry(
            advice.plan,
            state,
            expired=advice.expired is not None,
            notes=advice.pool_notes,
            plan_note=advice.plan_note,
        )
        with self._lock:
            self._enter_turn(advice.turn)
            if advice.lane == "deep":
                self._deep, self._deep_pending = entry, False
            else:
                self._ai = entry
            self._commit()

    def ai_summary(
        self, calls: int, input_tokens: int, output_tokens: int, off_plan: int = 0
    ) -> None:
        """本局 AI 用量只在终端显示。"""

    def show_plan(self, state: GameState, plan: GamePlan, previous: GamePlan | None) -> None:
        """整局计划定下或改了：招募时精简模式多一行"计划"，详细模式"本回合"里一行。"""
        with self._lock:
            self._game_plan = plan
            self._commit()

    def show_position(self, state: GameState, advice: PositionAdvice | None) -> None:
        """站位建议；None 表示现在不提示（还没买完、功能关闭），清掉旧的。"""
        with self._lock:
            if advice is not None:
                self._enter_turn(advice.turn)
            self._position = advice
            self._commit()

    # ---- 复盘 ----

    def show_review(self, state: GameState, review: GameReview) -> None:
        """对局结束后的复盘；已经开始下一局时不显示（晚到的结果只在终端和文件里）。"""
        with self._lock:
            current = self._state
            if current is None or current.phase != Phase.GAME_OVER:
                return
            self._review = review
            self._commit()

    # ---- 本版本统计 ----

    def show_stats(self, state: GameState, note: StatsNote) -> None:
        """这组备选英雄或饰品的本版本统计；只在签名（选项编号）对得上时显示。"""
        with self._lock:
            self._stats_note = note
            self._commit()

    # ---- 选英雄 ----

    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None:
        with self._lock:
            if advice.source == "rules":
                self._hero_rules = advice
            else:
                self._hero_ai = _HeroEntry(advice)
            self._commit()

    # ---- 选项 ----

    def show_choice(self, state: GameState, advice: ChoiceAdvice | None) -> None:
        """advice 为 None：游戏刚弹出一组选项（选项那一行由 on_update 的状态画出来）。"""
        if advice is None:
            return
        with self._lock:
            self._choice_ai = _ChoiceEntry(advice)
            self._commit()

    # ---- 内部（调用时已持有锁） ----

    def _enter_turn(self, turn: int | None) -> None:
        """换到另一个回合（或离开对局）时清空规则和 AI 内容。"""
        if turn != self._turn:
            self._turn = turn
            self._rules = None
            self._ai = None
            self._deep = None
            self._deep_pending = False
            self._hero_rules = None
            self._hero_ai = None
            self._choice_ai = None
            self._position = None
            self._stats_note = None

    def _remaining(self, entry: _PlanEntry, state: GameState) -> Plan:
        if state is entry.base or state.turn != entry.base.turn:
            return entry.plan
        try:
            return remaining_plan(entry.plan, entry.base, state)
        except Exception:
            log.debug("计算剩余步骤时出错，显示完整方案", exc_info=True)
            return entry.plan

    def _view(
        self,
        title: str,
        state: GameState,
        step: NextStep | None,
        blocks: tuple[Block, ...],
        source: str = "",
        source_note: str = "",
        notice: str | None = None,
    ) -> OverlayView:
        return OverlayView(
            title=title,
            compact=False,
            next_step=step,
            source=source,
            source_note=source_note,
            blocks=tuple(block for block in blocks if block.lines),
            notice=notice,
            warning=UNCERTAIN if state.uncertain else None,
        )

    def _notice(self, status: object, showing_ai: bool) -> str | None:
        """底部的一行提示，只写一条：AI 被关掉的常驻提示 → 这一路 AI 的错误 → 本回合规则
        建议出错（只在招募时）。面板显示的是 AI 的建议时不写前两条。"""
        if not showing_ai:
            if self._ai_off is not None:
                return self._ai_off[0]
            error = _error_text(status)
            if error is not None:
                return error
        state = self._state
        recruiting = state is not None and state.phase == Phase.RECRUIT and state.choice is None
        return RULES_ERROR if recruiting and self._rules == RULES_ERROR else None

    def _build(self) -> OverlayView:
        state = self._state
        if self._message is not None:
            return OverlayView(title=self._message)
        if state is None or state.phase == Phase.WAITING:
            return OverlayView(title=IDLE)
        if state.phase == Phase.HERO_SELECT:
            return self._hero_view(state)
        if state.phase == Phase.GAME_OVER:
            return self._game_over_view(state)
        if state.phase == Phase.COMBAT:
            return self._combat_view(state)
        if state.choice is not None:
            return self._choice_view(state, state.choice)
        return self._recruit_view(state)

    def _game_over_view(self, state: GameState) -> OverlayView:
        place = f" · 第 {state.final_place} 名" if state.final_place else ""
        title = f"对局结束{place}"
        if self._review is None:
            return OverlayView(title=title)
        lines = review_lines(self._review, detail=self._detail)
        block = Block("review", lines, label=REVIEW) if self._detail else Block("lines", lines)
        return OverlayView(title=title, compact=False, blocks=(block,))

    # ---- 对手 ----

    def _overview(self, state: GameState) -> OpponentOverview | None:
        """面板每次重画都自己整理对手一览（战斗中阵容截到后也要更新）；关掉、没有对手的数据
        或者出错时为 None。"""
        if not self._opponents_enabled or not state.opponents:
            return None
        try:
            return summarize_opponents(state)
        except Exception:
            if not self._overview_failed:
                self._overview_failed = True
                log.warning("整理对手一览时出错，面板不显示对手", exc_info=True)
            return None

    def _hero(self, s: OpponentSummary) -> str:
        return self._name(s.hero_card_id) if s.hero_card_id else "未知"

    def _combat_view(self, state: GameState) -> OverlayView:
        """战斗中：标题带正在打的对手；最强威胁不是他时下面一行"最强"。关掉对手一览、
        没有对手的数据时只有一行标题。不写战斗结果。"""
        title = f"第 {state.turn} 回合 · 战斗中"
        overview = self._overview(state)
        if overview is None:
            return OverlayView(title=title)
        current = next((s for s in overview.opponents if s.next), None)
        if current is not None and current.hero_card_id:
            title += f" · 对手 {self._name(current.hero_card_id)}"
        best = next((s for s in overview.opponents if s.strongest), None)
        if self._detail:
            block = Block("opponents", self._combat_lines(overview), label=OPPONENTS)
        elif best is None or best.next:
            return OverlayView(title=title)
        else:
            line = Line(f"{self._hero(best)} · {_board_brief(best)}", "dim", label=STRONGEST)
            block = Block("lines", (line,), small=True)
        return OverlayView(
            title=title,
            compact=False,
            blocks=(block,),
            warning=UNCERTAIN if state.uncertain else None,
        )

    def _combat_lines(self, overview: OpponentOverview) -> tuple[Line, ...]:
        """详细模式战斗中的"对手"块：正在打的（"对手"）、最强威胁（"最强"；是同一个人时合成
        "对手·最强"）、其他活着的按名次（小字），已淘汰的合成一行。"""
        turn, name = overview.turn, self._name
        lines = []
        current = next((s for s in overview.opponents if s.next), None)
        best = next((s for s in overview.opponents if s.strongest), None)
        if current is not None:
            text = _opponent_text(
                current, name, turn, place=True, seen=True, threat=current.strongest
            )
            lines.append(Line(f"{'对手·最强' if current.strongest else '对手'} {text}"))
        if best is not None and not best.next:
            text = _opponent_text(best, name, turn, place=True, seen=False, threat=True)
            lines.append(Line(f"{STRONGEST} {text}"))
        for s in overview.opponents:
            if s.alive and not s.next and not s.strongest:
                text = _opponent_text(s, name, turn, place=True, seen=True, threat=False)
                lines.append(Line(text, "dim", "opponent", small=True))
        gone = eliminated_names(overview, name)
        if gone:
            lines.append(Line("已淘汰：" + "、".join(gone), "muted", "opponent", small=True))
        return tuple(lines)

    def _opponents_line(self, state: GameState) -> Line | None:
        """详细模式"留意"里的对手一行：下个对手和最强威胁（是同一个人时写"下个对手也是最强"）。
        关掉对手、没有对手的数据时为 None。"""
        overview = self._overview(state)
        if overview is None:
            return None
        nxt = next((s for s in overview.opponents if s.next), None)
        best = next((s for s in overview.opponents if s.strongest), None)
        parts = []
        if nxt is not None:
            parts.append(f"下个对手 {self._hero(nxt)} {_board_brief(nxt)}")
        if best is not None:
            parts.append(
                NEXT_IS_STRONGEST
                if best.next
                else f"{STRONGEST} {self._hero(best)} {_board_brief(best)}"
            )
        if not parts:
            return None
        return Line(NAME_SEP.join(parts), "dim", "aside", max_lines=ASIDE_LINES)

    def _watch_block(self, state: GameState, recruiting: bool) -> Block:
        """详细模式的"留意"块（小字）：招募时一行要找和对子，一行对手；弹出选项时只有对手。"""
        lines = []
        if recruiting:
            parts = []
            found = (
                e for e in (self._deep, self._ai) if isinstance(e, _PlanEntry) and e.plan.look_for
            )
            entry = next(found, None)
            if entry is not None:
                parts.append(_look_for_text(entry.plan.look_for, entry.notes))
            rules = self._rules
            pairs = _pairs_text(rules.notes) if isinstance(rules, _PlanEntry) else ""
            if pairs:
                parts.append(pairs)
            if parts:
                lines.append(Line(NAME_SEP.join(parts), "dim", "aside", max_lines=ASIDE_LINES))
        opponents = self._opponents_line(state)
        if opponents is not None:
            lines.append(opponents)
        return Block("aside", tuple(lines), label=WATCH, small=True)

    # ---- 选项 ----

    def _choice_advice(self, choice: Choice) -> ChoiceAdvice | None:
        entry = self._choice_ai
        if isinstance(entry, _ChoiceEntry) and entry.advice.choice_id == choice.id:
            return entry.advice
        return None

    def _trinket_note(self, choice: Choice) -> StatsNote | None:
        note = self._stats_note
        if note is not None and note.kind == "trinket" and note.choice_id == choice.id:
            return note
        return None

    def _choice_view(self, state: GameState, choice: Choice) -> OverlayView:
        """弹出选项：选哪个、理由，一行按顺序列出选项（★ AI 推荐，▲ 本版本战绩最好的饰品）。
        招募的"然后""目标"在选完后回来。"""
        step = self._choice_step(choice)
        picked = step.target is not None
        source = SOURCE_AI if picked else ""
        waiting = ""
        if step.text == WAIT_CHOICE:
            waiting = REPAIRING if self._choice_ai == REPAIRING else PENDING
        star = step.target.index if picked else None
        note = self._trinket_note(choice)
        best = note.best if note is not None else None
        title = choice_title(choice, self._name)

        def marks(option) -> str:
            return (STAR if option.index == star else "") + (
                BEST if option.entity_id == best else ""
            )

        if self._detail:
            stats = {row.entity_id: row.text for row in note.rows} if note is not None else {}
            lines = []
            for o in choice.options:
                text = f"{marks(o)} {self._name(o.card_id)}" if marks(o) else self._name(o.card_id)
                tone = "text" if o.index == star else "dim"
                lines.append(Line(text, tone, "stats", right=stats.get(o.entity_id, "")))
            head = f"{title} · {len(choice.options)} 选 1"
            if note is not None:
                head += f" · {STATS_SOURCE}"
            choose = Block("choice", tuple(lines), label=CHOOSE, note=head)
            blocks = (choose, self._watch_block(state, recruiting=False))
        else:
            names = NAME_SEP.join(marks(o) + self._name(o.card_id) for o in choice.options)
            line = Line(names, "dim", "aside", label=title, max_lines=ASIDE_LINES)
            blocks = _single_block([line])
        return self._view(
            _recruit_title(state),
            state,
            step,
            blocks,
            source,
            _with_note(source, waiting),
            self._notice(self._choice_ai, picked),
        )

    def _choice_step(self, choice: Choice) -> NextStep:
        advice = self._choice_advice(choice)
        count = len(choice.options)
        option = None
        if advice is not None:
            option = next((o for o in choice.options if o.entity_id == advice.pick), None)
        if advice is not None and option is not None:
            return NextStep(
                text=f"{POINT}选 左数第 {option.index + 1} 个 {self._name(option.card_id)}",
                target=Target(row="choice", index=option.index, count=count),
                slots=slots_text("choice", option.index, count),
                reason=advice.reason,
                badge=CHOICE_BADGE,
            )
        failed = _error_text(self._choice_ai) is not None
        if failed or not self._choice_enabled or self._ai_off is not None or advice is not None:
            return NextStep(SELF_CHOICE, tone="muted")
        return NextStep(WAIT_CHOICE, tone="muted")

    # ---- 招募 ----

    def _ai_coming(self) -> bool:
        """AI 还会给方案：开着、没有因为 Key 无效或次数用完而关掉。"""
        return self._ai_enabled and self._ai_off is None

    def _unaffordable(self, plan: Plan, state: GameState) -> bool:
        hero = state.me.hero_card_id if state.me is not None else None
        return _first_step_unaffordable(plan, state, self._buy_cost(hero))

    def _current_plan(self, state: GameState) -> tuple[_PlanEntry | None, PlanSource, bool]:
        """面板上现在显示的方案、它来自哪一路，以及是不是在等新的 AI 建议：本回合的深度分析
        （没过期、商店没刷新、剩余的第一步做得起）→ 快速建议（商店没刷新、第一步做得起；
        商店刷新了而 AI 还会来时等新的建议）→ 规则（规则出错或还没来时为 None）。
        规则的方案不按"做得起"跳过：运行器在铸币变化时重算，它总是新的。"""
        deep = self._deep
        if (
            deep is not None
            and not deep.expired
            and not _shop_refreshed(deep.base, state)
            and not self._unaffordable(self._remaining(deep, state), state)
        ):
            return deep, "deep", False
        fast = self._ai
        if isinstance(fast, _PlanEntry):
            if not _shop_refreshed(fast.base, state):
                if not self._unaffordable(self._remaining(fast, state), state):
                    return fast, "fast", False
            elif self._ai_coming():
                return None, "fast", True
        rules = self._rules
        return (rules if isinstance(rules, _PlanEntry) else None), "rules", False

    def _outdated(self, entry: _PlanEntry, plan: Plan, state: GameState) -> bool:
        """面板上留着的步骤已经做不了、不挡站位那一步。运行器回合中途不再发"无需操作"，所以
        铸币花在了别处时旧方案会一直留着：
        - 规则方案里还有买、卖：运行器只在现在的规则方案里没有买、卖时才给站位，留着的是旧的；
        - 剩余的第一步做不起了（比如只剩最后的刷新，铸币已经不够）。
        AI 的方案不算前一种（规则不买的牌 AI 可能会买，比如酒馆法术）；后一种在挑方案时
        已经跳过了。"""
        if entry is self._rules and any(a.type in ("buy", "sell") for a in entry.plan.actions):
            return True
        return self._unaffordable(plan, state)

    def _recruit(self, state: GameState) -> _Recruit:
        """算"下一步"：方案剩下的第一步 → 站位的第一步 → 做完了 / 无需操作（剩下的第一步
        做不起时也是"无需操作"）；等建议时站位不插队。"""
        entry, source, waiting = self._current_plan(state)
        moves = tuple(self._pending_moves(state))
        if waiting:
            step = NextStep(WAIT_NEW, tone="muted")
            return _Recruit(step, None, source, True, None, (), moves, "status")
        if entry is None:
            # 规则出错、AI 又不会来（没开、Key 无效、次数用完）：不会再有方案，直接是站位
            if moves and not self._ai_coming():
                step = self._position_step(state, moves)
                return _Recruit(step, None, source, False, None, (), moves, "move")
            step = NextStep(WAIT, tone="muted")
            return _Recruit(step, None, source, False, None, (), moves, "status")
        plan = self._remaining(entry, state)
        numbers = self._numbers(entry, plan)
        if moves and (not plan.actions or self._outdated(entry, plan, state)):
            # 方案还剩步却让给了站位：那些步已经做不了（旧的），详细模式不再列
            step = self._position_step(state, moves)
            stale = bool(plan.actions)
            return _Recruit(step, entry, source, False, plan, numbers, moves, "move", stale)
        if not plan.actions:
            step = NextStep(ALL_DONE if entry.plan.actions else NOTHING_TO_DO, tone="muted")
            return _Recruit(step, entry, source, False, plan, numbers, moves, "status")
        if self._unaffordable(plan, state):
            # 只会是规则的方案（AI 的做不起时已经让路）：运行器回合中途不发"无需操作"，铸币花在
            # 了别处时旧方案的"买"一直留着，做不了，不当"下一步"
            step = NextStep(NOTHING_TO_DO, tone="muted")
            return _Recruit(step, entry, source, False, plan, numbers, moves, "status", stale=True)
        # 高亮框上的编号是它在完整方案里的序号（和面板、终端里的 ①② 对应）
        step = self._action_step(plan.actions[0], state, str(numbers[0] + 1))
        return _Recruit(step, entry, source, False, plan, numbers, moves, "plan")

    @staticmethod
    def _numbers(entry: _PlanEntry, plan: Plan) -> tuple[int, ...]:
        """剩下的每一步在完整方案里的序号（0 起）。"""
        full = entry.plan.actions
        numbers = []
        for action in plan.actions:
            index = next((i for i, a in enumerate(full) if a is action), None)
            if index is None:
                index = full.index(action) if action in full else 0
            numbers.append(index)
        return tuple(numbers)

    def _recruit_source(self, r: _Recruit) -> tuple[str, str]:
        """页眉右侧的来源和后面的灰字：显示快速建议时深度分析还在等、显示规则时快速或深度
        还在等（或者在改正）写"分析中…"（"改正中…"）；在等新的 AI 建议时只写"分析中…"。"""
        if r.waiting:
            return "", PENDING
        if r.entry is not None and r.source == "deep":
            return SOURCE_DEEP, ""
        if r.entry is not None and r.source == "fast":
            return SOURCE_AI, _with_note(SOURCE_AI, PENDING if self._deep_pending else "")
        waiting = _waiting_text(self._ai)
        if not waiting and self._deep_pending:
            waiting = PENDING
        source = SOURCE_RULES if r.entry is not None else ""
        return source, _with_note(source, waiting)

    def _ai_thoughts(self) -> _PlanEntry | None:
        """本回合最新的 AI 方案（深度优先，其次快速；过期、买不起、商店刷新了也算：思路没有
        过期，只是操作做不了），要有一句话总结或分析。"""
        for entry in (self._deep, self._ai):
            if isinstance(entry, _PlanEntry) and (entry.plan.summary or entry.plan.analysis):
                return entry
        return None

    def _goal_line(self) -> Line | None:
        """写"目标"：AI 的一句话总结（没有总结时用分析，截到 MAX_REASON 个字）；本回合没有任何
        AI 方案时用规则的"方向"。精简模式是行首标签，详细模式写成"目标：……"。"""
        thoughts = self._ai_thoughts()
        rules = self._rules
        any_ai = isinstance(self._deep, _PlanEntry) or isinstance(self._ai, _PlanEntry)
        if thoughts is not None:
            label, text = GOAL, _clip(thoughts.plan.summary or thoughts.plan.analysis, MAX_REASON)
            lines = GOAL_LINES
        elif not any_ai and isinstance(rules, _PlanEntry) and rules.plan.direction:
            label, text, lines = DIRECTION, rules.plan.direction, 1
        else:
            return None
        if self._detail:
            return Line(f"{label}：{text}", role="goal")
        return Line(text, role="goal", label=label, max_lines=lines)

    def _plan_line(self, state: GameState) -> Line | None:
        """整局计划一行；还没有计划时为 None。精简模式："计划"标签，"畸变神明 · 保血 · 核心
        蚀脑怪✓、飘荡祭品"（还没定时"未定 · 前期 · 想找 甲、乙"），一行；详细模式："计划：
        畸变神明（保血） · 核心 …… · 升本节奏"，和目标同级。"""
        plan = self._game_plan
        if plan is None:
            return None
        keys = key_cards_text(plan, state, self._name)
        cards = f"{'想找' if plan.undecided else '核心'} {keys}" if keys else ""
        if self._detail:
            head = f"{plan.comp}（{plan.stage}）" if plan.stage else plan.comp
            parts = [head, cards, plan.tiers]
            return Line("计划：" + NAME_SEP.join(p for p in parts if p), role="goal", max_lines=2)
        parts = [plan.comp, plan.stage or "", cards]
        text = NAME_SEP.join(p for p in parts if p)
        return Line(text, "dim", "aside", label=PLAN, max_lines=1)

    def _plan_note_line(self, state: GameState, r: _Recruit) -> Line | None:
        """详细模式的"提醒：商店里有核心卡 X，方案却没买它"：显示的 AI 方案出建议时有提醒，而且
        按现在的局面和计划还是这样（买了那张、铸币花完了就不再提醒）。"""
        entry, plan = r.entry, r.plan
        if entry is None or entry.plan_note is None or plan is None or state.me is None:
            return None
        ctx = build_context(state, self._buy_cost(state.me.hero_card_id))
        if ctx is None:
            return None
        try:
            note = off_plan_note(plan, self._game_plan, ctx, state, self._name)
        except Exception:
            log.debug("检查计划提醒时出错", exc_info=True)
            return None
        return Line(f"提醒：{note}", "muted", "alt") if note is not None else None

    def _then_text(self, state: GameState, r: _Recruit) -> str:
        """写"然后"：显示的方案在第一步之后的每一步（编号是完整方案里的序号，短写），后面还有
        站位要调时接"· 站位 把 X 拖到最左边"（前面没有步骤时不带"· "）；"下一步"是站位时
        是站位的后几步。"""
        parts = []
        card_of = self._card_of(state)
        if r.on == "plan" and r.plan is not None:
            for action, number in zip(r.plan.actions[1:], r.numbers[1:], strict=True):
                what = describe_action(action, state, self._name, reasons=False, short=True)
                parts.append(f"{_number(number)} {what}")
            if r.moves:
                moves = "、".join(self._move_text(m, card_of) for m, _ in r.moves)
                parts.append(f"{'· ' if parts else ''}站位 {moves}")
        elif r.on == "move":
            for i, (move, _) in enumerate(r.moves[1:], start=1):
                parts.append(f"{_number(i)} {self._move_text(move, card_of)}")
        return STEP_SEP.join(parts)

    def _recruit_view(self, state: GameState) -> OverlayView:
        r = self._recruit(state)
        source, source_note = self._recruit_source(r)
        if self._detail:
            plan = Block("plan", self._plan_lines(state, r), label=THIS_TURN)
            blocks = (plan, self._watch_block(state, recruiting=True))
        else:
            lines = []
            then = self._then_text(state, r)
            if then:
                lines.append(Line(then, "dim", "aside", label=THEN, max_lines=ASIDE_LINES))
            goal = self._goal_line()
            if goal is not None:
                lines.append(goal)
            plan_line = self._plan_line(state)
            if plan_line is not None:
                lines.append(plan_line)
            blocks = _single_block(lines)
        showing_ai = r.entry is not None and r.source != "rules"
        return self._view(
            _recruit_title(state),
            state,
            r.step,
            blocks,
            source,
            source_note,
            self._notice(self._ai, showing_ai),
        )

    def _plan_lines(self, state: GameState, r: _Recruit) -> tuple[Line, ...]:
        """详细模式的"本回合"：显示的方案剩下的每一步（完整写法，"下一步"那一步是正文色，
        后面的暗一档；做完了写一行状态），站位，目标，最后一行"规则另议"。"""
        lines = []
        entry, plan = r.entry, r.plan
        if entry is not None and plan is not None and not r.stale:
            steps = list(zip(plan.actions, r.numbers, strict=True))
            for i, (action, number) in enumerate(steps[:MAX_ACTIONS]):
                text = (
                    f"{_number(number)} {describe_action(action, state, self._name, reasons=False)}"
                )
                current = i == 0 and r.on == "plan"
                lines.append(Line(text, "text" if current else "dim", "step"))
            if len(steps) > MAX_ACTIONS:
                lines.append(Line("…", "dim", "step"))
            if not steps:
                if not entry.plan.actions:
                    status = NOTHING_TO_DO
                elif _shop_refreshed(entry.base, state):
                    status = REFRESHED  # 刷新后买的那几张不在商店里了，不能说成"已按建议操作完"
                else:
                    status = DONE
                lines.append(Line(status, "muted", "step"))
        position = self._position_line(state, r)
        if position is not None:
            lines.append(position)
        goal = self._goal_line()
        if goal is not None:
            lines.append(goal)
        for extra in (self._plan_line(state), self._plan_note_line(state, r)):
            if extra is not None:
                lines.append(extra)
        alt = self._rules_alt(state, r)
        if alt is not None:
            lines.append(alt)
        return tuple(lines)

    def _position_line(self, state: GameState, r: _Recruit) -> Line | None:
        """详细模式的"站位：把 X 拖到最左边 · 为什么"："下一步"已经是站位时只写后面的步；
        两步时编号。"""
        moves = r.moves[1:] if r.on == "move" else r.moves
        if not moves:
            return None
        card_of = self._card_of(state)
        first = len(r.moves) - len(moves)
        parts = []
        for i, (move, _) in enumerate(moves, start=first):
            text = f"{self._move_text(move, card_of)} · {move.short}"
            parts.append(f"{_number(i)} {text}" if len(r.moves) > 1 else text)
        return Line("站位：" + STEP_SEP.join(parts), "dim")

    def _rules_alt(self, state: GameState, r: _Recruit) -> Line | None:
        """写"规则另议：买 X · 用技能 定罪"：显示的是 AI 的方案时，规则方案剩下的步里 AI 没有的
        那些（短写，一行）。显示的是规则的方案时没有这一行。"""
        rules = self._rules
        if r.entry is None or r.source == "rules" or r.plan is None:
            return None
        if not isinstance(rules, _PlanEntry):
            return None
        ai = {_action_key(a) for a in r.plan.actions}
        other = [a for a in self._remaining(rules, state).actions if _action_key(a) not in ai]
        if not other:
            return None
        texts = (describe_action(a, state, self._name, reasons=False, short=True) for a in other)
        return Line("规则另议：" + NAME_SEP.join(texts), "muted", "alt", max_lines=1)

    # ---- 站位 ----

    def _pending_moves(self, state: GameState) -> list[tuple[Move, int]]:
        """站位建议里还没做完的步，和要拖的随从现在在第几个（0 起）。场上的随从和出建议时
        不一样（又买了、卖了）时为空：等运行器的新结果。"""
        advice, me = self._position, state.me
        if advice is None or me is None or advice.turn != state.turn:
            return []
        order = [m.entity_id for m in me.board]
        if sorted(order) != sorted(advice.board):
            return []
        pending = []
        for move in advice.moves:
            if move.entity_id not in order:
                continue
            index = order.index(move.entity_id)
            if not _move_done(move, index, order):
                pending.append((move, index))
        return pending

    def _card_of(self, state: GameState) -> dict[int, str]:
        me = state.me
        return {m.entity_id: m.card_id for m in me.board} if me is not None else {}

    def _move_text(self, move: Move, card_of: dict[int, str]) -> str:
        """写成"把 孢林卵怪 拖到最左边"。"""
        return f"把 {self._name(card_of[move.entity_id])} {where_text(move, card_of, self._name)}"

    def _position_step(self, state: GameState, moves: tuple[tuple[Move, int], ...]) -> NextStep:
        """站位的第一步：框住要拖的随从，小圆点后面写拖到哪，理由是一句"为何"。"""
        move, index = moves[0]
        card_of = self._card_of(state)
        where = where_text(move, card_of, self._name)
        count = len(card_of)
        return NextStep(
            text=f"{POINT}把 左数第 {index + 1} 个 {self._name(card_of[move.entity_id])} {where}",
            target=Target(row="board", index=index, count=count),
            slots=f"{slots_text('board', index, count)}   {where}",
            reason=move.short,
            badge=BADGE_MOVE,
            reason_label=WHY,
        )

    def _action_step(self, action: Action, state: GameState, badge: str) -> NextStep:
        me = state.me
        match action.type:
            case "buy" | "sell":
                row = "shop" if action.type == "buy" else "board"
                cards = state.shop if row == "shop" else (me.board if me is not None else ())
                verb = "买" if row == "shop" else "卖"
                index = next(
                    (i for i, c in enumerate(cards) if c.entity_id == action.entity_id), None
                )
                if index is None:
                    return NextStep(f"{POINT}{verb} #{action.entity_id}", badge=badge)
                card = cards[index]
                what = self._name(card.card_id)
                if getattr(card, "card_type", "MINION") != "MINION":
                    what += "（法术）"
                return NextStep(
                    text=f"{POINT}{verb} 左数第 {index + 1} 个 {what}",
                    target=Target(row=row, index=index, count=len(cards)),
                    slots=slots_text(row, index, len(cards)),
                    badge=badge,
                )
            case "upgrade":
                tier = f"{me.tavern_tier + 1} 本" if me is not None else "下一本"
                cost = me.upgrade_cost if me is not None else None
                price = f"（{cost} 金）" if cost is not None else ""
                text = f"{POINT}升本 → {tier}{price}"
                return NextStep(text, Target(button="upgrade"), badge=badge)
            case "refresh":
                return NextStep(f"{POINT}刷新商店", Target(button="refresh"), badge=badge)
            case "freeze":
                return NextStep(f"{POINT}冻结商店", Target(button="freeze"), badge=badge)
            case _:
                hp = me.hero_power if me is not None else None
                text = f"{POINT}用英雄技能"
                if hp is not None:
                    text += f" {self._name(hp.card_id)}"
                    if hp.needs_target is not False and action.entity_id is not None:
                        card = _entity_card(state, action.entity_id)
                        text += f" → {self._name(card) if card else f'#{action.entity_id}'}"
                return NextStep(text, Target(button="hero_power"), badge=badge)

    # ---- 选英雄 ----

    def _hero_advice(self, select: HeroSelect) -> HeroAdvice | None:
        """AI 对这一组备选的推荐（重随前的不算）。"""
        entry = self._hero_ai
        if isinstance(entry, _HeroEntry) and entry.advice.signature == select.signature():
            return entry.advice
        return None

    def _hero_view(self, state: GameState) -> OverlayView:
        select = state.hero_select
        if select is None:
            # 还没发备选英雄（英雄是占位的），或者已经选好、在等第 1 回合
            hero = state.me.hero_card_id if state.me is not None else None
            if hero and hero != HERO_PLACEHOLDER:
                return OverlayView(title=f"已选 {self._name(hero)} · 等待招募阶段")
            return OverlayView(title=IDLE)
        rules = self._hero_rules
        star = rules.pick if rules is not None and rules.signature == select.signature() else None
        note = self._stats_note
        if note is not None and (note.kind != "hero" or note.signature != select.signature()):
            note = None  # 上一组备选的（重随了）
        ai = self._hero_advice(select)
        by_ai = ai is not None and ai.pick is not None
        step = self._hero_step(select, ai.pick if by_ai else star, ai.reason if by_ai else "")
        source = "" if step.target is None else SOURCE_AI if by_ai else SOURCE_RULES
        pick = step.target.index if step.target is not None else None
        rows = {row.entity_id: row for row in note.rows} if note is not None else {}
        if self._detail:
            blocks = self._hero_blocks(state, select, pick, rows, star if by_ai else None, ai)
            if note is not None:
                blocks = (replace(blocks[0], note=STATS_SOURCE), *blocks[1:])
        else:
            heroes = []
            for i, h in enumerate(select.offered):
                if note is None:
                    number = str(h.armor) if h.armor > 0 else "未知"
                elif h.entity_id in rows:
                    number = f"{rows[h.entity_id].score:.2f}"
                else:
                    number = armor_zh(h.armor)  # 查不到统计的写护甲
                heroes.append(f"{STAR if i == pick else ''}{self._name(h.card_id)} {number}")
            label = PLACE if note is not None else ARMOR
            line = Line(NAME_SEP.join(heroes), "dim", "aside", label=label, max_lines=ASIDE_LINES)
            blocks = _single_block([line])
        return self._view(
            HERO_STATS_TITLE if note is not None else HERO_TITLE,
            state,
            step,
            blocks,
            source,
            _with_note(source, _waiting_text(self._hero_ai)),
            self._notice(self._hero_ai, by_ai),
        )

    def _hero_blocks(
        self,
        state: GameState,
        select: HeroSelect,
        pick: int | None,
        rows: dict,
        rules_pick: int | None,
        ai: HeroAdvice | None,
    ) -> tuple[Block, ...]:
        """详细模式选英雄："备选"每个英雄一行（★ 是"下一步"指的那个，右列护甲或战绩），AI 推荐
        的和规则的 ★ 不是同一个时末尾"规则另选"；AI 到了再加"点评"块（小字）。
        rules_pick：AI 推荐了别的英雄时规则推荐的那个，否则为 None。"""
        lines = []
        for i, h in enumerate(select.offered):
            name = self._name(h.card_id)
            right = rows[h.entity_id].text if h.entity_id in rows else armor_zh(h.armor)
            tone = "text" if i == pick else "dim"
            lines.append(Line(f"{STAR} {name}" if i == pick else name, tone, "stats", right=right))
        other = next((h for h in select.offered if h.entity_id == rules_pick), None)
        if other is not None and ai is not None and ai.pick != rules_pick:
            lines.append(
                Line(f"规则另选：{self._name(other.card_id)}", "muted", "alt", max_lines=1)
            )
        blocks = [Block("heroes", tuple(lines), label=CANDIDATES)]
        if ai is not None:
            comments = hero_comment_lines(state, ai, self._name)
            lines = tuple(Line(text, "dim", "comment") for text in comments)
            blocks.append(Block("comments", lines, label=COMMENTS, small=True))
        return tuple(blocks)

    def _hero_step(self, select: HeroSelect, pick: int | None, reason: str) -> NextStep:
        """AI 的推荐优先（带理由），没有时用规则的 ★。"""
        offered = select.offered
        index = next((i for i, h in enumerate(offered) if h.entity_id == pick), None)
        if index is None:
            return NextStep(WAIT, tone="muted")
        count = len(offered)
        return NextStep(
            text=f"{POINT}选 左数第 {index + 1} 个 {self._name(offered[index].card_id)}",
            target=Target(row="hero", index=index, count=count),
            slots=slots_text("hero", index, count),
            reason=reason,
            badge=CHOICE_BADGE,
        )

    def _commit(self) -> None:
        content = replace(self._build(), banner=self._banners.current(self._state, self._name))
        if content != self._content:
            self._content = content
            self._version += 1
