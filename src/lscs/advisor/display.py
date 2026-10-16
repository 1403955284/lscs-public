"""把建议格式化成终端文字；DisplayGroup 把建议同时发给多个显示端（终端、悬浮面板）。"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from typing import Protocol

from lscs.advisor.core_cards import CORE, CoreMarks
from lscs.advisor.models import (
    Action,
    Advice,
    ChoiceAdvice,
    Expired,
    GamePlan,
    HeroAdvice,
    Lane,
    Plan,
    PositionAdvice,
    StatsNote,
)
from lscs.advisor.names import choice_option_text, minion_label, tribe_zh
from lscs.advisor.opponents import (
    OpponentOverview,
    OpponentSummary,
    contested_parts,
    eliminated_names,
)
from lscs.advisor.plan_check import comp_with_tribe, key_cards_text, plain_name
from lscs.advisor.pool import PoolNote, note_for
from lscs.advisor.positioning import INTRO, step_texts
from lscs.review.report import GameReview, ai_terminal_lines, terminal_lines
from lscs.state.models import ChoiceKind, GameState

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
Output = Callable[[str], None]
CIRCLED = "①②③④⑤⑥⑦⑧⑨"
INDENT = "          "  # 让多步方案的后续行与第一步对齐
ICONS: dict[Lane, str] = {
    "fast": "🤖 ",
    "deep": "🧠 ",
    "hero": "🤖 ",
    "choice": "🤖 ",
    "review": "🧠 ",
}
EXPIRED_LABELS: dict[Expired, str] = {
    "refreshed": "操作已过期（商店刷新了），只看思路",
    "invalid": "操作不可用，只看思路",
    "changed": "操作已过期，只看思路",
}


def _print(line: str) -> None:
    print(line, flush=True)


def _describe_entity(entity_id: int, state: GameState, name: Namer, short: bool = False) -> str:
    """场面、商店、手牌里的实体：随从带身材（short 时不带）；都找不到时用编号。"""
    me = state.me
    cards = [*(me.board if me else ()), *state.shop, *(me.hand if me else ())]
    card = next((c for c in cards if c.entity_id == entity_id), None)
    if card is None:
        return f"#{entity_id}"
    if not short and getattr(card, "card_type", "MINION") == "MINION" and card.atk is not None:
        return f"{name(card.card_id)} {card.atk}/{card.health}"
    return name(card.card_id)


def describe_action(
    action: Action, state: GameState, name: Namer, reasons: bool = True, short: bool = False
) -> str:
    """一步操作的写法。short：悬浮面板的短写（"然后"一行、"规则另议"），随从不带身材、
    法术不写价格、"使用英雄技能"写成"用技能"；终端和面板的步骤列表用完整写法。"""
    me = state.me
    match action.type:
        case "buy":
            item = next((s for s in state.shop if s.entity_id == action.entity_id), None)
            if item is None:
                what = f"#{action.entity_id}"
            elif item.card_type == "MINION":
                what = name(item.card_id)
                if not short:
                    what += f" {item.atk}/{item.health}"
            else:
                price = f"，{item.cost} 金" if item.cost is not None and not short else ""
                what = f"{name(item.card_id)}（法术{price}）"
            text = f"买 {what}"
        case "sell":
            board = me.board if me is not None else ()
            m = next((m for m in board if m.entity_id == action.entity_id), None)
            if m is None:
                what = f"#{action.entity_id}"
            else:
                what = name(m.card_id) if short else f"{name(m.card_id)} {m.atk}/{m.health}"
            text = f"卖 {what}"
        case "upgrade":
            tier = me.tavern_tier + 1 if me is not None else "?"
            text = f"升本 → {tier} 本"
        case "hero_power":
            hp = me.hero_power if me is not None else None
            text = "用技能" if short else "使用英雄技能"
            if hp is not None:
                text += f" {name(hp.card_id)}"
            if hp is not None and hp.needs_target is not False and action.entity_id is not None:
                text += f" → {_describe_entity(action.entity_id, state, name, short)}"
        case "refresh":
            text = "刷新"
        case _:
            text = "冻结"
    return f"{text}（{action.reason}）" if action.reason and reasons else text


def format_plan(plan: Plan, state: GameState, name: Namer, reasons: bool = True) -> list[str]:
    """第一行不带前缀；多步时每步一行。reasons=False 时不带理由（悬浮面板用）。"""
    if not plan.actions:
        return [f"无需操作（{plan.summary}）" if plan.summary and reasons else "无需操作"]
    return [
        f"{CIRCLED[i] if i < len(CIRCLED) else i + 1} {describe_action(a, state, name, reasons)}"
        for i, a in enumerate(plan.actions)
    ]


def deep_wait_hint(thinking: str) -> str:
    """深度分析大约要等多久（终端提示和启动时的说明共用）。"""
    return "约 40 秒" if thinking == "high" else "约 20 秒"


def one_line(text: str) -> str:
    """AI 写的多行文字合成一行。"""
    return " ".join(text.split())


def pairs_line(notes: Sequence[PoolNote]) -> str | None:
    """规则块里的"对子"一行：对子写"×N"，差一张成对子的写"×1 商店有"；没有时为 None。"""
    parts = []
    for note in notes:
        if note.have >= 2:
            parts.append(f"{note.name} ×{note.have}（池里还剩约 {note.left} 张）")
        elif note.pair:
            parts.append(f"{note.name} ×1 商店有（还剩约 {note.left} 张）")
    return "对子：" + "；".join(parts) if parts else None


def look_for_text(look_for: Sequence[str], notes: Sequence[PoolNote]) -> str:
    """要找的牌名：查得到张数的后面加"（还剩约 N 张）"。"""
    parts = []
    for text in look_for:
        note = note_for(text, notes)
        parts.append(f"{text}（还剩约 {note.left} 张）" if note is not None else text)
    return "、".join(parts)


def thought_lines(
    plan: Plan,
    summary: bool = True,
    extra: tuple[str, ...] = (),
    notes: Sequence[PoolNote] = (),
) -> list[str]:
    """AI 方案下面的"分析"一行（没有分析时用"思路"），以及"方向 / 要找"一行。
    notes：要找的牌在池里还剩约几张。"""
    lines = []
    if plan.analysis:
        lines.append(f"分析：{one_line(plan.analysis)}")
    elif plan.summary and summary:
        lines.append(f"思路：{one_line(plan.summary)}")
    tail = []
    if plan.direction:
        tail.append(f"方向：{plan.direction}")
    if plan.look_for:
        tail.append("要找：" + look_for_text(plan.look_for, notes))
    tail += extra
    if tail:
        lines.append("   ".join(tail))
    return lines


def plan_terminal_lines(
    plan: GamePlan, previous: GamePlan | None, state: GameState | None, name: Namer
) -> list[str]:
    """终端的 🗺 块（整局计划与流派指南设计文档第 8.1 节）。第一行：定下时（包括从"未定"定下
    目标流派）"计划：……"，改了时"计划改了：……"（升本节奏没变就不重复写），流派和核心卡没变、
    只有阶段变了时"阶段：保血（原来 过渡）"。下面一行"原因：……"（改了且 AI 写了原因时），再一行
    "规则：……   换方向：……"（定下时都写，改了时只写变了的）。"""
    if previous is not None and (
        plan.signature() == previous.signature() or (plan.undecided and previous.undecided)
    ):
        lines = [f"阶段：{plan.stage or '—'}（原来 {previous.stage or '—'}）"]
        if plan.changed:
            lines.append(f"原因：{plan.changed}")
        return lines
    if previous is not None and previous.undecided:
        previous = None  # 从"未定"定下目标流派：和第一次定下一样写

    def fresh(field: str) -> str:
        """这个字段要写（定下时有就写，改了时变了才写）；不写时为空。"""
        value = getattr(plan, field)
        return value if previous is None or value != getattr(previous, field) else ""

    parts = [comp_with_tribe(plan)]
    keys = key_cards_text(plan, state, name)
    if keys:
        parts.append(f"{'想找' if plan.undecided else '核心'} {keys}")
    if fresh("tiers"):
        parts.append(plan.tiers)
    if plan.stage:
        parts.append(f"阶段 {plan.stage}")
    lines = [("计划：" if previous is None else "计划改了：") + " · ".join(parts)]
    if previous is not None and plan.changed:
        lines.append(f"原因：{plan.changed}")
    tail = []
    if fresh("roll"):
        tail.append(f"规则：{plan.roll}")
    if fresh("pivot"):
        tail.append(f"换方向：{plan.pivot}")
    if tail:
        lines.append("   ".join(tail))
    return lines


def hero_name(state: GameState, entity_id: int | None, name: Namer) -> str:
    """备选英雄的名字；不在当前备选里时用编号。"""
    select = state.hero_select
    offered = select.offered if select is not None else ()
    hero = next((h for h in offered if h.entity_id == entity_id), None)
    return name(hero.card_id) if hero is not None else f"#{entity_id}"


def hero_comment_lines(state: GameState, advice: HeroAdvice, name: Namer) -> list[str]:
    """AI 对每个英雄的点评，按备选顺序，跳过没有点评的（推荐的那个有点评也列出）。"""
    comments = {c.entity_id: c.text for c in advice.comments if c.text}
    select = state.hero_select
    offered = select.offered if select is not None else ()
    return [
        f"{name(h.card_id)}：{comments[h.entity_id]}" for h in offered if h.entity_id in comments
    ]


def choice_pick_text(state: GameState, pick: int, name: Namer) -> str:
    """选项的写法，如"左数第 3 个 泰瑞尔"；不在当前这组选项里时用编号。"""
    options = state.choice.options if state.choice is not None else ()
    option = next((o for o in options if o.entity_id == pick), None)
    if option is None:
        return f"#{pick}"
    return f"左数第 {option.index + 1} 个 {name(option.card_id)}"


def position_lines(advice: PositionAdvice, state: GameState, name: Namer) -> list[str]:
    """站位的每一步一行："把 左数第 N 个 X 拖到最左边（为什么）"；两步时编号，还有没显示的
    步时最后一步后面加"；先做这两步"。"""
    me = state.me
    card_of = {m.entity_id: m.card_id for m in me.board} if me is not None else {}
    texts = step_texts(advice, card_of, name)
    lines = [
        f"把 左数第 {t.number} 个 {name(t.card_id)} {t.where}（{t.move.reason}）" for t in texts
    ]
    if len(lines) > 1:
        lines = [f"{CIRCLED[i]} {line}" for i, line in enumerate(lines)]
    if advice.more and lines:
        lines[-1] += "；先做这两步"
    return lines


def opponent_marks(summary: OpponentSummary) -> str:
    """▶ 下一个对手、★ 最强威胁；两个都是时 ▶★，都不是时为空。"""
    return ("▶" if summary.next else "") + ("★" if summary.strongest else "")


def _place(summary: OpponentSummary) -> str:
    return f"第 {summary.place if summary.place is not None else '?'} 名"


def _hero(summary: OpponentSummary, name: Namer) -> str:
    return name(summary.hero_card_id) if summary.hero_card_id else "未知"


def opponent_line(summary: OpponentSummary, state: GameState, name: Namer) -> str:
    """终端 👥 块里一个对手的一行（不带前面的标记）。"""
    head = f"{_place(summary)} {_hero(summary, name)}"
    if not summary.alive:
        if not summary.next:
            return f"{head} 已淘汰"
        opp = state.opponents.get(summary.player_id)
        board = opp.last_board if opp is not None else ()
        if summary.seen_turn is None:
            return f"{head} 已淘汰（残影）· 还没见过阵容"
        if not board:
            return f"{head} 已淘汰（残影）· 第 {summary.seen_turn} 回合见到空场"
        cards = "、".join(minion_label(m, name) for m in board)
        return f"{head} 已淘汰（残影）· 第 {summary.seen_turn} 回合见到的阵容：{cards}"
    tier = "?" if summary.tavern_tier is None else summary.tavern_tier
    parts = [f"{head} {summary.health}+{summary.armor}", f"{tier} 本"]
    if summary.seen_turn is None:
        parts.append("还没见过阵容")
    elif summary.empty_board:
        parts.append(f"第 {summary.seen_turn} 回合见到空场")
    else:
        stats = f"身材 {summary.total_stats}"
        if summary.main_tribe is not None:
            stats = f"{tribe_zh(summary.main_tribe)}×{summary.tribe_count} {stats}"
        parts.append(stats)
        if summary.key_cards:
            parts.append("、".join(minion_label(m, name) for m in summary.key_cards))
        parts.append(f"第 {summary.seen_turn} 回合见")
    parts.append(f"威胁 {summary.threat}")
    return " · ".join(parts)


# 弹出时标核心卡的选项（饰品、黑暗之赐、技能升级不标：不是随从，或随从本身不是选择的重点）
CORE_CHOICES = frozenset({ChoiceKind.TRIPLE, ChoiceKind.DISCOVER})


def core_groups(marks: CoreMarks) -> list[tuple[str, int, str]]:
    """商店里的核心卡按卡名合并：(卡名, 张数, why)，按第一次出现的位置。"""
    counts: dict[str, int] = {}
    whys: dict[str, str] = {}
    for mark in marks.marks:
        counts[mark.name] = counts.get(mark.name, 0) + 1
        whys.setdefault(mark.name, mark.why)
    return [(card, count, whys[card]) for card, count in counts.items()]


def core_count(card: str, count: int) -> str:
    return f"{card} ×{count}" if count > 1 else card


def core_line(marks: CoreMarks) -> str | None:
    """终端的一行"◆ 核心卡：飘荡祭品 ×2（计划）"，几张不同的卡用"；"连；商店里没有时 None。"""
    groups = core_groups(marks)
    if not groups:
        return None
    return f"{CORE} 核心卡：" + "；".join(f"{core_count(n, c)}（{why}）" for n, c, why in groups)


def core_options(state: GameState, marks: CoreMarks, name: Namer) -> list[str]:
    """弹出的三连 / 发现里是核心卡的随从选项的卡名（按选项顺序）；别的选项种类没有。"""
    choice = state.choice
    if choice is None or choice.kind not in CORE_CHOICES:
        return []
    names = set(marks.names)
    found = []
    for option in choice.options:
        card = plain_name(name(option.card_id))
        if option.card_type == "MINION" and card in names and card not in found:
            found.append(card)
    return found


def opponents_header(overview: OpponentOverview) -> str:
    parts = contested_parts(overview)
    if overview.unknown:
        parts.append(f"未知 {overview.unknown}")
    return f"👥 对手（主打：{'、'.join(parts)}）" if parts else "👥 对手"


class AdviceSink(Protocol):
    """AdviceRunner 输出建议的对象：终端（AdviceDisplay）、悬浮面板或 DisplayGroup。"""

    def show_opponents(self, state: GameState, overview: OpponentOverview) -> None: ...
    def show_rules(self, state: GameState, advice: Advice) -> None: ...
    def rules_error(self, hero: bool = False) -> None: ...
    def ai_pending(self, lane: Lane = "fast") -> None: ...
    def ai_text(self, text: str, lane: Lane = "fast") -> None: ...
    def ai_repairing(self, lane: Lane, reason: str) -> None: ...
    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None: ...
    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None: ...
    def show_choice(self, state: GameState, advice: ChoiceAdvice | None) -> None: ...
    def show_plan(self, state: GameState, plan: GamePlan, previous: GamePlan | None) -> None: ...
    def ai_summary(
        self, calls: int, input_tokens: int, output_tokens: int, off_plan: int = 0
    ) -> None: ...
    def show_review(self, state: GameState, review: GameReview) -> None: ...
    def show_position(self, state: GameState, advice: PositionAdvice | None) -> None: ...
    def show_stats(self, state: GameState, note: StatsNote) -> None: ...
    def show_core_cards(self, state: GameState, marks: CoreMarks | None) -> None: ...


def stats_labels(state: GameState, note: StatsNote, name: Namer) -> dict[int, str]:
    """条目对应的名字：备选英雄写名字，饰品写"名字（N 金）"。"""
    if note.kind == "hero":
        select = state.hero_select
        offered = select.offered if select is not None else ()
        return {h.entity_id: name(h.card_id) for h in offered}
    options = state.choice.options if state.choice is not None else ()
    return {o.entity_id: choice_option_text(o, name) for o in options}


class AdviceDisplay:
    def __init__(self, name: Namer, out: Output = _print, deep_thinking: str = "low") -> None:
        self._name = name
        self._out = out
        self._deep_pending = f"深度分析中…（{deep_wait_hint(deep_thinking)}）"
        self._review_pending = f"AI 复盘生成中…（{deep_wait_hint(deep_thinking)}）"
        self._last_rules: tuple | None = None
        # 最近一次打印过的站位：(回合, 场上的实体集合, 每一步的 (实体, 槽位))
        self._last_position: tuple[int, frozenset[int], tuple] | None = None
        self._last_core: tuple | None = None  # 最近一次打印过的商店核心卡：(回合, 标记)
        self._last_core_choice: int | None = None  # 打印过"选项里有核心卡"的那组选项

    def _block(self, label: str, lines: list[str]) -> None:
        self._out(f"  {label}{lines[0]}")
        for line in lines[1:]:
            self._out(f"{INDENT}{line}")

    def show_opponents(self, state: GameState, overview: OpponentOverview) -> None:
        """每回合开始时的对手一览：活着的对手每人一行，▶ 下一个对手、★ 最强威胁；下一个对手
        是已淘汰的玩家（残影）时它单独一行，其他淘汰的合成一行"已淘汰：……"。"""
        self._out(f"  {opponents_header(overview)}")
        for summary in overview.opponents:
            if not summary.alive and not summary.next:
                continue
            marks = opponent_marks(summary)
            prefix = f"  {marks} " if len(marks) == 2 else f"   {marks or ' '} "
            self._out(prefix + opponent_line(summary, state, self._name))
        gone = eliminated_names(overview, self._name)
        if gone:
            self._out("     已淘汰：" + "、".join(gone))

    def show_rules(self, state: GameState, advice: Advice) -> None:
        key = (advice.turn, advice.plan.signature(), advice.pool_notes)
        if key == self._last_rules:
            return
        self._last_rules = key
        lines = format_plan(advice.plan, state, self._name)
        if advice.plan.direction:
            lines[-1] += f"   方向：{advice.plan.direction}"
        pairs = pairs_line(advice.pool_notes)
        if pairs is not None:
            lines.append(pairs)
        if state.uncertain:
            lines.append("⚠ 状态可能不准")
        self._block("📋 规则：", lines)

    def show_position(self, state: GameState, advice: PositionAdvice | None) -> None:
        """站位建议（站位建议设计文档第 6.1 节）：同一回合同样的几步只打一次；本局第一次先打
        一行机制说明；照着拖完（场上还是那几个随从、没有要动的了）打一行"站位已调好"。
        advice 为 None（还没买完、功能关闭）或没有步时不打印。新的一局（回合数变小了，或者
        带着本局第一次的机制说明）先忘掉上一局打过的站位。"""
        if advice is None:
            return
        entities = frozenset(advice.board)
        steps = tuple((m.entity_id, m.index) for m in advice.moves)
        last = self._last_position
        if last is not None and (advice.intro or advice.turn < last[0]):
            last = self._last_position = None
        if not steps:
            if last is not None and last[:2] == (advice.turn, entities) and last[2]:
                self._last_position = (advice.turn, entities, ())
                self._out("  🧭 站位已调好")
            return
        if last is not None and (last[0], last[2]) == (advice.turn, steps):
            return
        self._last_position = (advice.turn, entities, steps)
        if advice.intro:
            self._out(f"  🧭 {INTRO}")
        self._block("🧭 站位：", position_lines(advice, state, self._name))

    def rules_error(self, hero: bool = False) -> None:
        """hero：选英雄的规则建议出错。"""
        self._out("  📋 选英雄规则建议出错" if hero else "  📋 本回合规则建议出错")

    def ai_pending(self, lane: Lane = "fast") -> None:
        self._out(("  🧠 " + self._deep_pending) if lane == "deep" else "  🤖 AI 分析中…")

    def ai_text(self, text: str, lane: Lane = "fast") -> None:
        """AI 的错误、提示、过期等一行消息。"""
        self._out(f"  {ICONS[lane]}{text}")

    def ai_repairing(self, lane: Lane, reason: str) -> None:
        """AI 的回答不合法，正在让它改正（AI 自我改正设计文档第 3.7 节）。reason 是校验器的原因。"""
        if lane == "deep":
            self._out(f"  🧠 深度分析的操作不合法（{reason}），正在让它改正…")
        else:
            self._out(f"  {ICONS[lane]}AI 建议不合法（{reason}），正在让它改正…")

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        """方案和思路；方案和整局计划矛盾时（plan_note）块末尾加一行"⚠ 计划提醒"。"""
        plan, deep = advice.plan, advice.lane == "deep"
        icon = ICONS[advice.lane]
        notes = advice.pool_notes
        reminder = [f"⚠ 计划提醒：{advice.plan_note}"] if advice.plan_note else []
        if advice.expired is not None:
            # 冒号而不是括号：原因里本身带括号，不嵌套
            label = f"深度分析：{EXPIRED_LABELS[advice.expired]}"
            self._block(icon, [label, *thought_lines(plan, notes=notes)])
            return
        if advice.elapsed is None:
            seconds = "?"
        else:  # 深度分析要等十几秒，不用精确到 0.1 秒
            seconds = f"{advice.elapsed:.0f} 秒" if deep else f"{advice.elapsed:.1f} 秒"
        if agrees:
            who = "深度分析" if deep else "AI "
            agree = f"✓ {who}同意规则建议（{seconds}）"
            self._block(icon, [agree, *thought_lines(plan, notes=notes), *reminder])
            return
        stale = (f"（{advice.stale_note}）",) if advice.stale_note else ()
        lines = format_plan(plan, state, self._name)
        lines += thought_lines(plan, summary=bool(plan.actions), extra=stale, notes=notes)
        label = f"深度分析（{seconds}）：" if deep else f"AI（{seconds}）："
        self._block(icon + label, lines + reminder)

    def show_plan(self, state: GameState, plan: GamePlan, previous: GamePlan | None) -> None:
        """整局计划定下或改了（只在这时打印，紧跟在带来它的深度分析之后）。"""
        self._block("🗺 ", plan_terminal_lines(plan, previous, state, self._name))

    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None:
        """选英雄建议。规则：一行；AI：推荐和理由一行，下面每个英雄的点评各一行。
        agrees：AI 和规则推荐的是同一个英雄。"""
        pick = hero_name(state, advice.pick, self._name)
        if advice.source == "rules":
            text = f"选 {pick}（{advice.reason}）" if advice.pick is not None else advice.reason
            self._out(f"  📋 规则：{text}")
            return
        seconds = "?" if advice.elapsed is None else f"{advice.elapsed:.1f} 秒"
        if agrees:
            first = f"✓ AI 也选 {pick}（{seconds}）：{advice.reason}"
        else:
            first = f"AI（{seconds}）：选 {pick} —— {advice.reason}"
        self._block("🤖 ", [first, *hero_comment_lines(state, advice, self._name)])

    def show_stats(self, state: GameState, note: StatsNote) -> None:
        """本版本统计（本版本统计设计文档第 6.3、7.1 节）：署名一行，下面每个条目一行，按平均
        名次从好到差，战绩最好的前面 ▲（只是战绩，不是推荐）；英雄配某族更好时行末写出来。"""
        what = "本版本战绩" if note.kind == "hero" else "饰品本版本战绩"
        self._out(f"  📊 {what}（{note.source}）：")
        labels = stats_labels(state, note, self._name)
        ranked = sorted(enumerate(note.rows), key=lambda pair: (pair[1].score, pair[0]))
        for _, row in ranked:
            label = labels.get(row.entity_id, f"#{row.entity_id}")
            mark = "▲ " if row.entity_id == note.best else "  "
            gap = "" if label.endswith("）") else " "  # "神秘魔方（5 金）第 3.95 名"
            fit = f" · {row.fit}" if row.fit else ""  # "配野兽更好"
            self._out(f"{INDENT}{mark}{label}{gap}{row.text}{fit}")

    def show_core_cards(self, state: GameState, marks: CoreMarks | None) -> None:
        """商店核心卡（流派指南自动更新设计文档第 5.2 节）：商店里有核心卡时一行，同样的标记在同一
        回合只打一次；弹出三连 / 发现、选项里有核心卡时一行（每组选项一次）。None 不打。"""
        if marks is None:
            return
        line = core_line(marks)
        key = (marks.turn, marks.marks)
        if line is not None and key != self._last_core:
            self._last_core = key
            self._out(f"  {line}")
        choice = state.choice
        if choice is None or choice.id == self._last_core_choice:
            return
        found = core_options(state, marks, self._name)
        if found:
            self._last_core_choice = choice.id
            parts = "、".join(f"{card}（{marks.why(card)}）" for card in found)
            self._out(f"  {CORE} 选项里有核心卡：{parts}")

    def show_choice(self, state: GameState, advice: ChoiceAdvice | None) -> None:
        """advice 为 None 表示游戏刚弹出一组选项：不打印（选项那一行由时间线打印）。"""
        if advice is None:
            return
        seconds = "?" if advice.elapsed is None else f"{advice.elapsed:.1f} 秒"
        pick = choice_pick_text(state, advice.pick, self._name)
        self._out(f"  🤖 AI（{seconds}）：选 {pick} —— {advice.reason}")

    def ai_summary(
        self, calls: int, input_tokens: int, output_tokens: int, off_plan: int = 0
    ) -> None:
        """off_plan：本局快速建议偏离计划（有"计划提醒"）的次数，0 次不写。"""
        tail = f"，快速建议偏离计划 {off_plan} 次" if off_plan else ""
        self._out(
            f"本局 AI 调用 {calls} 次，输入 {input_tokens / 1000:.1f} 千、"
            f"输出 {output_tokens / 1000:.1f} 千 token{tail}"
        )

    def show_review(self, state: GameState, review: GameReview) -> None:
        """对局复盘（对局复盘设计文档第 7.1 节）：先是规则版的一块（要调 AI 时末尾一行"生成中"），
        AI 复盘到了再打一块，失败时一行。"""
        if review.ai_state in ("done", "failed"):
            lines = ai_terminal_lines(review)
        else:
            lines = terminal_lines(review)
            if review.ai_state == "pending":
                lines.append(f"  🧠 {self._review_pending}")
        for line in lines:
            self._out(line)


class DisplayGroup:
    """把每条建议依次转发给多个显示端；一个显示端出错只记日志，不影响其他显示端。"""

    def __init__(self, *displays: AdviceSink) -> None:
        self._displays = displays

    def _each(self, method: str, *args: object) -> None:
        for display in self._displays:
            try:
                getattr(display, method)(*args)
            except Exception:
                log.warning("显示建议时出错（%s）", method, exc_info=True)

    def show_opponents(self, state: GameState, overview: OpponentOverview) -> None:
        self._each("show_opponents", state, overview)

    def show_rules(self, state: GameState, advice: Advice) -> None:
        self._each("show_rules", state, advice)

    def rules_error(self, hero: bool = False) -> None:
        self._each("rules_error", hero)

    def ai_pending(self, lane: Lane = "fast") -> None:
        self._each("ai_pending", lane)

    def ai_text(self, text: str, lane: Lane = "fast") -> None:
        self._each("ai_text", text, lane)

    def ai_repairing(self, lane: Lane, reason: str) -> None:
        self._each("ai_repairing", lane, reason)

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        self._each("show_ai", state, advice, agrees)

    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None:
        self._each("show_hero", state, advice, agrees)

    def show_choice(self, state: GameState, advice: ChoiceAdvice | None) -> None:
        self._each("show_choice", state, advice)

    def show_plan(self, state: GameState, plan: GamePlan, previous: GamePlan | None) -> None:
        self._each("show_plan", state, plan, previous)

    def ai_summary(
        self, calls: int, input_tokens: int, output_tokens: int, off_plan: int = 0
    ) -> None:
        self._each("ai_summary", calls, input_tokens, output_tokens, off_plan)

    def show_review(self, state: GameState, review: GameReview) -> None:
        self._each("show_review", state, review)

    def show_position(self, state: GameState, advice: PositionAdvice | None) -> None:
        self._each("show_position", state, advice)

    def show_stats(self, state: GameState, note: StatsNote) -> None:
        self._each("show_stats", state, note)

    def show_core_cards(self, state: GameState, marks: CoreMarks | None) -> None:
        self._each("show_core_cards", state, marks)
