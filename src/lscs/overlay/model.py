"""悬浮面板的数据模型。

监听线程调用 on_update 和建议方法（与 AdviceDisplay 相同），界面线程定时调用 snapshot()；
所有读写都在锁里。内容变化时版本号加一，界面只在版本号变化时重画。

终端是日志，面板是"现在该做什么"：规则和 AI 的方案连同出建议时的局面一起保存，
每次状态更新都去掉已经做完的步骤，再按当前局面重新排版。

选英雄时（选英雄建议设计文档第 7.3 节）：规则区按备选顺序列出英雄和护甲，★ 标在规则推荐的
前面；AI 区是推荐和理由，下面每个英雄一句点评。只显示基于当前这组备选的建议（重随后换组）。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from lscs.advisor.display import format_plan, hero_comment_lines, hero_name
from lscs.advisor.models import Advice, HeroAdvice, Lane, Plan
from lscs.advisor.names import armor_zh
from lscs.advisor.runner import remaining_plan
from lscs.llm.notices import AUTH_DISABLED, BUDGET_EXHAUSTED
from lscs.llm.parse import MAX_ACTIONS  # 每个方案最多显示几步：和 AI 回答的上限一致
from lscs.overlay.geometry import STEP_SEP
from lscs.state.build import HERO_PLACEHOLDER
from lscs.state.models import Event, EventType, GameState, HeroSelect, Phase

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
Tone = Literal["text", "muted"]
# 面板太高时：先去掉 direction，再去掉 look_for、note 和 comment（选英雄的点评）、
# 把 reason 截成一行（见 condense）
Role = Literal["main", "direction", "look_for", "reason", "note", "comment"]

IDLE = "lscs 已就绪，等待招募阶段"
UNCERTAIN = "⚠ 状态可能不准"
DONE = "已按建议操作完"
NOTHING_TO_DO = "无需操作"
REFRESHED = "商店已刷新"
AI_REFRESHED = "商店已刷新，等待新的 AI 建议"
MAX_REASON = 40  # "目标：……"（没有目标时"分析：……"）整行最多几个字，超出加"…"
MAX_LOOK_FOR = 24  # "要找：……"整行最多几个字，超出加"…"
HERO_TITLE = "选英雄 · 护甲越少越强"
PENDING = "分析中…"
AI_TITLE = "AI"
DEEP_TITLE = "AI（深度）"
DEEP_PENDING = "深度分析中…"
# 调度器关闭 AI 的提示 → 之后回合常驻的一行（以及是否在新的一局清除）
AI_OFF = {
    AUTH_DISABLED: ("AI 已关闭（Key 无效）", False),
    BUDGET_EXHAUSTED: ("本局 AI 次数已用完", True),
}


@dataclass(frozen=True)
class Line:
    text: str
    tone: Tone = "text"  # muted：提示、错误（灰色）
    role: Role = "main"


@dataclass(frozen=True)
class OverlayView:
    title: str
    compact: bool = True  # 只有标题行（战斗中、没有对局）
    rules: tuple[Line, ...] = ()
    ai: tuple[Line, ...] = ()
    ai_title: str = AI_TITLE  # 有本回合的深度分析时是"AI（深度）"
    warning: str | None = None
    version: int = 0


def condense(view: OverlayView, level: int) -> OverlayView:
    """面板超高时逐级精简：1 去掉"方向"；2 再去掉"要找"、附注（如"基于 N 秒前的局面"）
    和选英雄的逐个点评。"目标"（或没有目标时的"分析"）截成一行由界面按像素处理。
    标题、动作、选英雄的推荐和警告总是保留。"""
    dropped: set[str] = set()
    if level >= 1:
        dropped.add("direction")
    if level >= 2:
        dropped.update(("look_for", "note", "comment"))

    def keep(lines: tuple[Line, ...]) -> tuple[Line, ...]:
        return tuple(line for line in lines if line.role not in dropped)

    return replace(view, rules=keep(view.rules), ai=keep(view.ai))


@dataclass(frozen=True)
class _PlanEntry:
    """一条方案建议，连同出建议时的局面。"""

    plan: Plan
    base: GameState
    thoughts: bool = False  # AI 的方案：显示"要找"和"目标"
    stale_note: str | None = None
    agrees: bool = False  # AI 同意规则建议：只写"✓ 同意规则建议"，不列动作
    expired: bool = False  # 深度分析的操作已经不能用，只看思路


Entry = _PlanEntry | tuple[Line, ...]


@dataclass(frozen=True)
class _HeroEntry:
    """AI 的选英雄建议。"""

    advice: HeroAdvice
    agrees: bool = False  # 和规则推荐的是同一个英雄


HeroEntry = _HeroEntry | tuple[Line, ...]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text[:limit] + "…" if len(text) > limit else text


def _thought_lines(plan: Plan) -> tuple[Line, ...]:
    """一行"要找"，一行"目标"。目标用 AI 的一句话总结（提示词要求 25 字以内）；
    完整的分析通常一百多字，截断后常停在半句话，所以只在没有总结时才退回显示"分析"。"""
    lines = []
    if plan.look_for:
        lines.append(
            Line(_clip("要找：" + "、".join(plan.look_for), MAX_LOOK_FOR), role="look_for")
        )
    if plan.summary:
        lines.append(Line(_clip("目标：" + plan.summary, MAX_REASON), role="reason"))
    elif plan.analysis:
        lines.append(Line(_clip("分析：" + plan.analysis, MAX_REASON), role="reason"))
    return tuple(lines)


def _shop_refreshed(base: GameState, state: GameState) -> bool:
    """出建议之后商店换了一批（同一回合、前后都有商品、没有一件相同）。"""
    if state.turn != base.turn or not base.shop or not state.shop:
        return False
    return not {s.entity_id for s in base.shop} & {s.entity_id for s in state.shop}


def _choosing(state: GameState | None) -> bool:
    """正在选英雄：手牌区里有备选英雄。"""
    return state is not None and state.phase == Phase.HERO_SELECT and state.hero_select is not None


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
    def __init__(self, name: Namer, ai_enabled: bool = True) -> None:
        self._name = name
        self._ai_enabled = ai_enabled
        self._lock = threading.Lock()
        self._version = 0
        self._content = OverlayView(title=IDLE)
        self._state: GameState | None = None
        self._turn: int | None = None  # 下面的规则和 AI 内容属于哪个回合
        self._rules: Entry = ()
        self._ai: Entry = ()  # 快速建议
        self._deep: _PlanEntry | None = None  # 本回合的深度分析
        self._deep_pending = False
        self._hero_rules: HeroAdvice | None = None  # 选英雄的规则建议
        self._hero_ai: HeroEntry = ()  # 选英雄的 AI 建议，或"分析中…"、错误提示
        self._ai_off: tuple[str, bool] | None = None  # (常驻提示, 新的一局是否清除)
        self._message: str | None = None

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
                self._hero_rules, self._hero_ai = None, ()  # 新对局（或重连）：重新开始
            if any(e.type == EventType.HEROES_OFFERED for e in events):
                self._hero_ai = ()  # 新的一组备选（重随）：上一组的推荐、错误和提示都不再显示
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

    def show_rules(self, state: GameState, advice: Advice) -> None:
        with self._lock:
            self._enter_turn(advice.turn)
            self._rules = _PlanEntry(advice.plan, state)
            self._commit()

    def rules_error(self, hero: bool = False) -> None:
        if hero:
            return  # 选英雄时规则区照样列出英雄和护甲，只是没有 ★（出错的提示在终端里）
        with self._lock:
            self._rules = (Line("本回合规则建议出错", "muted"),)
            self._commit()

    def ai_pending(self, lane: Lane = "fast") -> None:
        with self._lock:
            if lane == "deep":
                self._deep, self._deep_pending = None, True
            elif lane == "hero":
                self._hero_ai = (Line(PENDING, "muted"),)
            else:
                self._ai = (Line(PENDING, "muted"),)
            self._commit()

    def ai_text(self, text: str, lane: Lane = "fast") -> None:
        with self._lock:
            if lane == "deep":
                self._deep_pending = False  # 深度分析出错、超时不影响快速建议，只在终端显示
            else:
                if text in AI_OFF:
                    self._ai_off = AI_OFF[text]
                # 调度器的提示（预算用完、局面出错）不分路：正在选英雄时放进选英雄的 AI 区
                if lane == "hero" or _choosing(self._state):
                    self._hero_ai = (Line(text, "muted"),)
                else:
                    self._ai = (Line(text, "muted"),)
            self._commit()

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        entry = _PlanEntry(
            advice.plan,
            state,
            thoughts=True,
            stale_note=advice.stale_note,
            agrees=agrees,
            expired=advice.expired is not None,
        )
        with self._lock:
            self._enter_turn(advice.turn)
            if advice.lane == "deep":
                self._deep, self._deep_pending = entry, False
            else:
                self._ai = entry
            self._commit()

    def ai_summary(self, calls: int, input_tokens: int, output_tokens: int) -> None:
        """本局 AI 用量只在终端显示。"""

    # ---- 选英雄 ----

    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None:
        with self._lock:
            if advice.source == "rules":
                self._hero_rules = advice
            else:
                self._hero_ai = _HeroEntry(advice, agrees)
            self._commit()

    # ---- 内部（调用时已持有锁） ----

    def _enter_turn(self, turn: int | None) -> None:
        """换到另一个回合（或离开对局）时清空规则和 AI 内容。"""
        if turn != self._turn:
            self._turn = turn
            self._rules = ()
            self._ai = ()
            self._deep = None
            self._deep_pending = False
            self._hero_rules = None
            self._hero_ai = ()

    def _remaining(self, entry: _PlanEntry, state: GameState) -> Plan:
        if state is entry.base or state.turn != entry.base.turn:
            return entry.plan
        try:
            return remaining_plan(entry.plan, entry.base, state)
        except Exception:
            log.debug("计算剩余步骤时出错，显示完整方案", exc_info=True)
            return entry.plan

    def _render(
        self, entry: Entry, state: GameState, is_ai: bool, deep: bool = False
    ) -> tuple[Line, ...]:
        """deep：深度分析的步骤做完后仍然显示它的思路。"""
        if isinstance(entry, tuple):
            return entry
        thoughts = _thought_lines(entry.plan) if entry.thoughts else ()
        if entry.agrees:
            return (Line("✓ 同意规则建议"), *thoughts)
        refreshed = _shop_refreshed(entry.base, state)
        if refreshed and is_ai:
            # 旧方案里的牌已经不在商店了；AI 还开着的话很快会有新建议，否则不显示
            ai_on = self._ai_enabled and self._ai_off is None
            return (Line(AI_REFRESHED, "muted"),) if ai_on else ()
        plan = self._remaining(entry, state)
        if not plan.actions:
            # 刷新后买的那几张不在商店里了，不能说成"已按建议操作完"；等规则的新建议
            done = REFRESHED if refreshed else DONE
            status = Line(done if entry.plan.actions else NOTHING_TO_DO, "muted")
            return (status, *thoughts) if deep else (status,)
        steps = format_plan(plan, state, self._name, reasons=False)
        if len(steps) > MAX_ACTIONS:
            steps = [*steps[:MAX_ACTIONS], "…"]
        lines = [Line(STEP_SEP.join(steps))]
        if plan.direction:
            lines.append(Line(f"方向：{plan.direction}", role="direction"))
        lines += thoughts
        if entry.stale_note:
            lines.append(Line(entry.stale_note, "muted", role="note"))
        return tuple(lines)

    def _render_ai(self, state: GameState) -> tuple[tuple[Line, ...], str]:
        """AI 区的内容和标题。有本回合的深度分析时优先显示它；它的操作已经过期（刷新过、
        不合法）时，只拿它的"方向""要找"和"目标"，配上快速建议现在的动作（或状态）和附注。"""
        fast = self._render(self._ai, state, is_ai=True)
        deep = self._deep
        if deep is None:
            return fast, AI_TITLE
        if not deep.expired and not _shop_refreshed(deep.base, state):
            return self._render(deep, state, is_ai=True, deep=True), DEEP_TITLE
        direction = deep.plan.direction
        thoughts = (
            *((Line(f"方向：{direction}", role="direction"),) if direction else ()),
            *_thought_lines(deep.plan),
        )
        if not thoughts:
            return fast, AI_TITLE
        actions = tuple(line for line in fast if line.role == "main")
        notes = tuple(line for line in fast if line.role == "note")
        return (*actions, *thoughts, *notes), DEEP_TITLE

    def _build(self) -> OverlayView:
        state = self._state
        if self._message is not None:
            return OverlayView(title=self._message)
        if state is None or state.phase == Phase.WAITING:
            return OverlayView(title=IDLE)
        if state.phase == Phase.HERO_SELECT:
            return self._hero_view(state)
        if state.phase == Phase.GAME_OVER:
            place = f" · 第 {state.final_place} 名" if state.final_place else ""
            return OverlayView(title=f"对局结束{place}")
        if state.phase == Phase.COMBAT:
            return OverlayView(title=f"第 {state.turn} 回合 · 战斗中")
        ai, ai_title = self._render_ai(state)
        if not ai and self._ai_off is not None:
            ai = (Line(self._ai_off[0], "muted"),)
        elif not ai and not self._ai_enabled:
            ai = (Line("AI 未开启", "muted"),)
        if self._deep_pending:
            ai = (*ai, Line(DEEP_PENDING, "muted"))
        return OverlayView(
            title=_recruit_title(state),
            compact=False,
            rules=self._render(self._rules, state, is_ai=False),
            ai=ai,
            ai_title=ai_title,
            warning=UNCERTAIN if state.uncertain else None,
        )

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
        rows = tuple(
            Line(
                f"{'★ ' if h.entity_id == star else ''}{self._name(h.card_id)}  {armor_zh(h.armor)}"
            )
            for h in select.offered
        )
        ai = self._render_hero_ai(state, select)
        if not ai and self._ai_off is not None:
            ai = (Line(self._ai_off[0], "muted"),)
        elif not ai and not self._ai_enabled:
            ai = (Line("AI 未开启", "muted"),)
        return OverlayView(
            title=HERO_TITLE,
            compact=False,
            rules=rows,
            ai=ai,
            warning=UNCERTAIN if state.uncertain else None,
        )

    def _render_hero_ai(self, state: GameState, select: HeroSelect) -> tuple[Line, ...]:
        entry = self._hero_ai
        if isinstance(entry, tuple):
            return entry
        advice = entry.advice
        if advice.signature != select.signature():
            return ()  # 上一组备选的推荐（重随了）
        pick = hero_name(state, advice.pick, self._name)
        first = f"{'✓ 也选' if entry.agrees else '选'} {pick}：{advice.reason}"
        comments = hero_comment_lines(state, advice, self._name)
        return (
            Line(_clip(first, MAX_REASON)),
            *(Line(text, role="comment") for text in comments),
        )

    def _commit(self) -> None:
        content = self._build()
        if content != self._content:
            self._content = content
            self._version += 1
