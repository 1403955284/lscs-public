"""悬浮面板的数据模型。

监听线程调用 on_update 和建议方法（与 AdviceDisplay 相同），界面线程定时调用 snapshot()；
所有读写都在锁里。内容变化时版本号加一，界面只在版本号变化时重画。

终端是日志，面板是"现在该做什么"：规则和 AI 的方案连同出建议时的局面一起保存，
每次状态更新都去掉已经做完的步骤，再按当前局面重新排版。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from lscs.advisor.display import format_plan
from lscs.advisor.models import Advice, Plan
from lscs.advisor.runner import remaining_plan
from lscs.llm.notices import AUTH_DISABLED, BUDGET_EXHAUSTED
from lscs.overlay.geometry import STEP_SEP
from lscs.state.models import Event, EventType, GameState, Phase

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
Tone = Literal["text", "muted"]
# 面板太高时：先去掉 direction，再去掉 note、把 reason 截成一行（见 condense）
Role = Literal["main", "direction", "reason", "note"]

IDLE = "lscs 已就绪，等待招募阶段"
UNCERTAIN = "⚠ 状态可能不准"
DONE = "已按建议操作完"
NOTHING_TO_DO = "无需操作"
REFRESHED = "商店已刷新"
AI_REFRESHED = "商店已刷新，等待新的 AI 建议"
MAX_ACTIONS = 4  # 每个方案最多显示几步
MAX_REASON = 40  # "思路：……"整行最多几个字，超出加"…"
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
    warning: str | None = None
    version: int = 0


def condense(view: OverlayView, level: int) -> OverlayView:
    """面板超高时逐级精简：1 去掉"方向"；2 再去掉附注（如"基于 N 秒前的局面"）。
    "思路"截成一行由界面按像素处理。标题、动作和警告总是保留。"""
    dropped: set[str] = set()
    if level >= 1:
        dropped.add("direction")
    if level >= 2:
        dropped.add("note")

    def keep(lines: tuple[Line, ...]) -> tuple[Line, ...]:
        return tuple(line for line in lines if line.role not in dropped)

    return replace(view, rules=keep(view.rules), ai=keep(view.ai))


@dataclass(frozen=True)
class _PlanEntry:
    """一条方案建议，连同出建议时的局面。"""

    plan: Plan
    base: GameState
    summary: str = ""
    stale_note: str | None = None


Entry = _PlanEntry | tuple[Line, ...]


def _reason_line(summary: str) -> Line:
    text = "思路：" + " ".join(summary.split())
    if len(text) > MAX_REASON:
        text = text[:MAX_REASON] + "…"
    return Line(text, role="reason")


def _shop_refreshed(base: GameState, state: GameState) -> bool:
    """出建议之后商店换了一批（同一回合、前后都有商品、没有一件相同）。"""
    if state.turn != base.turn or not base.shop or not state.shop:
        return False
    return not {s.entity_id for s in base.shop} & {s.entity_id for s in state.shop}


def _recruit_title(state: GameState) -> str:
    parts = [f"第 {state.turn} 回合"]
    me = state.me
    if me is not None:
        parts += [f"铸币 {me.gold}", f"{me.tavern_tier} 本"]
        # 满本时没有升本按钮，所以没有升本费用
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
        self._ai: Entry = ()
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

    def rules_error(self) -> None:
        with self._lock:
            self._rules = (Line("本回合规则建议出错", "muted"),)
            self._commit()

    def ai_pending(self) -> None:
        self._set_ai((Line("分析中…", "muted"),))

    def ai_text(self, text: str) -> None:
        with self._lock:
            if text in AI_OFF:
                self._ai_off = AI_OFF[text]
            self._ai = (Line(text, "muted"),)
            self._commit()

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        if agrees:
            entry: Entry = (Line("✓ 同意规则建议"),)
        else:
            entry = _PlanEntry(advice.plan, state, advice.plan.summary, advice.stale_note)
        with self._lock:
            self._enter_turn(advice.turn)
            self._ai = entry
            self._commit()

    def ai_summary(self, calls: int, input_tokens: int, output_tokens: int) -> None:
        """本局 AI 用量只在终端显示。"""

    # ---- 内部（调用时已持有锁） ----

    def _set_ai(self, lines: tuple[Line, ...]) -> None:
        with self._lock:
            self._ai = lines
            self._commit()

    def _enter_turn(self, turn: int | None) -> None:
        """换到另一个回合（或离开对局）时清空规则和 AI 内容。"""
        if turn != self._turn:
            self._turn = turn
            self._rules = ()
            self._ai = ()

    def _remaining(self, entry: _PlanEntry, state: GameState) -> Plan:
        if state is entry.base or state.turn != entry.base.turn:
            return entry.plan
        try:
            return remaining_plan(entry.plan, entry.base, state)
        except Exception:
            log.debug("计算剩余步骤时出错，显示完整方案", exc_info=True)
            return entry.plan

    def _render(self, entry: Entry, state: GameState, is_ai: bool) -> tuple[Line, ...]:
        if isinstance(entry, tuple):
            return entry
        refreshed = _shop_refreshed(entry.base, state)
        if refreshed and is_ai:
            # 旧方案里的牌已经不在商店了；AI 还开着的话很快会有新建议，否则不显示
            ai_on = self._ai_enabled and self._ai_off is None
            return (Line(AI_REFRESHED, "muted"),) if ai_on else ()
        plan = self._remaining(entry, state)
        if not plan.actions:
            # 刷新后买的那几张不在商店里了，不能说成"已按建议操作完"；等规则的新建议
            done = REFRESHED if refreshed else DONE
            return (Line(done if entry.plan.actions else NOTHING_TO_DO, "muted"),)
        steps = format_plan(plan, state, self._name, reasons=False)
        if len(steps) > MAX_ACTIONS:
            steps = [*steps[:MAX_ACTIONS], "…"]
        lines = [Line(STEP_SEP.join(steps))]
        if plan.direction:
            lines.append(Line(f"方向：{plan.direction}", role="direction"))
        if entry.summary:
            lines.append(_reason_line(entry.summary))
        if entry.stale_note:
            lines.append(Line(entry.stale_note, "muted", role="note"))
        return tuple(lines)

    def _build(self) -> OverlayView:
        state = self._state
        if self._message is not None:
            return OverlayView(title=self._message)
        if state is None or state.phase in (Phase.WAITING, Phase.HERO_SELECT):
            return OverlayView(title=IDLE)
        if state.phase == Phase.GAME_OVER:
            place = f" · 第 {state.final_place} 名" if state.final_place else ""
            return OverlayView(title=f"对局结束{place}")
        if state.phase == Phase.COMBAT:
            return OverlayView(title=f"第 {state.turn} 回合 · 战斗中")
        ai = self._render(self._ai, state, is_ai=True)
        if not ai and self._ai_off is not None:
            ai = (Line(self._ai_off[0], "muted"),)
        elif not ai and not self._ai_enabled:
            ai = (Line("AI 未开启", "muted"),)
        return OverlayView(
            title=_recruit_title(state),
            compact=False,
            rules=self._render(self._rules, state, is_ai=False),
            ai=ai,
            warning=UNCERTAIN if state.uncertain else None,
        )

    def _commit(self) -> None:
        content = self._build()
        if content != self._content:
            self._content = content
            self._version += 1
