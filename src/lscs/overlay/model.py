"""悬浮面板的数据模型。

监听线程调用 on_update 和建议方法（与 AdviceDisplay 相同），界面线程定时调用 snapshot()；
所有读写都在锁里。内容变化时版本号加一，界面只在版本号变化时重画。

终端是日志，面板是"现在该做什么"：规则和 AI 的方案连同出建议时的局面一起保存，
每次状态更新都去掉已经做完的步骤，再按当前局面重新排版。

选英雄时（选英雄建议设计文档第 7.3 节）：规则区按备选顺序列出英雄和护甲，★ 标在规则推荐的
前面；AI 区是推荐和理由，下面每个英雄一句点评。只显示基于当前这组备选的建议（重随后换组）。

"下一步"（选项建议设计文档第 7 节）：面板最上面只写现在该做的一步，和面板上显示的方案一致
（选项 → 选英雄 → 深度 → 快速 → 规则），带上画面高亮要框的位置（Target）。游戏弹出"选一个"
时多一个"选择"区，★ 标在 AI 推荐的前面；那组选完就消失。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from lscs.advisor.display import format_plan, hero_comment_lines, hero_name
from lscs.advisor.models import Action, Advice, ChoiceAdvice, HeroAdvice, Lane, Plan
from lscs.advisor.names import armor_zh, choice_title
from lscs.advisor.runner import remaining_plan
from lscs.llm.notices import AUTH_DISABLED, BUDGET_EXHAUSTED
from lscs.llm.parse import MAX_ACTIONS  # 每个方案最多显示几步：和 AI 回答的上限一致
from lscs.overlay.geometry import STEP_SEP
from lscs.state.build import HERO_PLACEHOLDER
from lscs.state.models import Choice, Event, EventType, GameState, HeroSelect, Phase

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
Tone = Literal["text", "muted"]
# 面板太高时：先去掉 direction，再去掉 look_for、note 和 comment（选英雄的点评）、
# 把 reason 截成一行（见 condense）
Role = Literal["main", "direction", "look_for", "reason", "note", "comment"]
Row = Literal["shop", "board", "choice", "hero"]  # 画面上的一排牌
Button = Literal["upgrade", "refresh", "freeze", "hero_power"]  # 画面上的按钮

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
REPAIRING = "改正中…"  # AI 的回答不合法，正在让它改正（还在等，和"分析中…"一样）
AI_TITLE = "AI"
DEEP_TITLE = "AI（深度）"
DEEP_PENDING = "深度分析中…"
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
CHOICE_BADGE = "选"  # 选项、选英雄的高亮框上写的字（方案的步骤写第几步）
ROW_LABELS: dict[str, str] = {"shop": "商店", "board": "场面", "choice": "选项", "hero": "英雄"}


@dataclass(frozen=True)
class Line:
    text: str
    tone: Tone = "text"  # muted：提示、错误（灰色）
    role: Role = "main"


@dataclass(frozen=True)
class Target:
    """画面高亮框住的地方：一排里的一个槽位，或者一个按钮。"""

    row: Row | None = None  # 哪一排
    index: int = 0  # 从左数第几个，0 起
    count: int = 0  # 这一排现在有几个（居中排版要用）
    button: Button | None = None


@dataclass(frozen=True)
class NextStep:
    """面板最上面的"下一步"：一句大字，一行小圆点标出位置，选项再加一行理由。"""

    text: str  # "▶ 买 左数第 2 个 飘荡祭品"
    target: Target | None = None  # 高亮画哪里；None 只显示文字
    slots: str = ""  # "商店 ○ ● ○ ○ ○"：没有目标排时为空
    reason: str = ""  # 选项的理由；方案的步骤不带理由（终端里有）
    tone: Tone = "text"  # muted：等待、做完了之类的状态
    badge: str = ""  # 高亮框上的编号：方案里的第几步，选项和选英雄写"选"


@dataclass(frozen=True)
class OverlayView:
    title: str
    compact: bool = True  # 只有标题行（战斗中、没有对局）
    rules: tuple[Line, ...] = ()
    ai: tuple[Line, ...] = ()
    ai_title: str = AI_TITLE  # 有本回合的深度分析时是"AI（深度）"
    warning: str | None = None
    version: int = 0
    next_step: NextStep | None = None  # compact 时为 None
    choice: tuple[Line, ...] = ()  # "选择"区：游戏正弹着"选一个"时才有


def slots_text(row: Row, index: int, count: int) -> str:
    dots = " ".join("●" if i == index else "○" for i in range(count))
    return f"{ROW_LABELS[row]} {dots}"


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

    # "下一步"的第一行和小圆点总是保留，理由由界面在第 2 级截成一行
    return replace(view, rules=keep(view.rules), ai=keep(view.ai), choice=keep(view.choice))


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


@dataclass(frozen=True)
class _ChoiceEntry:
    """AI 的选项建议。"""

    advice: ChoiceAdvice


ChoiceEntry = _ChoiceEntry | tuple[Line, ...]


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


def _entity_card(state: GameState, entity_id: int | None) -> str | None:
    """场面、商店、手牌里的实体的卡牌 ID。"""
    me = state.me
    cards = [*(me.board if me else ()), *state.shop, *(me.hand if me else ())]
    return next((c.card_id for c in cards if c.entity_id == entity_id), None)


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
    def __init__(self, name: Namer, ai_enabled: bool = True, choice_ai: bool = True) -> None:
        """choice_ai：弹出选项时会不会调用 AI（config.toml 的 choice_advice）。"""
        self._name = name
        self._ai_enabled = ai_enabled
        self._choice_enabled = ai_enabled and choice_ai
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
        self._choice_ai: ChoiceEntry = ()  # 选项的 AI 建议，或"分析中…"、错误提示
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
            if state.choice is None or any(e.type == EventType.CHOICE_OFFERED for e in events):
                self._choice_ai = ()  # 选完了，或者换了一组
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
            elif lane == "choice":
                self._choice_ai = (Line(PENDING, "muted"),)
            else:
                self._ai = (Line(PENDING, "muted"),)
            self._commit()

    def ai_text(self, text: str, lane: Lane = "fast") -> None:
        with self._lock:
            if lane == "deep":
                self._deep_pending = False  # 深度分析出错、超时不影响快速建议，只在终端显示
            elif lane == "choice":
                self._choice_ai = (Line(text, "muted"),)
            else:
                if text in AI_OFF:
                    self._ai_off = AI_OFF[text]
                # 调度器的提示（预算用完、局面出错）不分路：正在选英雄时放进选英雄的 AI 区
                if lane == "hero" or _choosing(self._state):
                    self._hero_ai = (Line(text, "muted"),)
                else:
                    self._ai = (Line(text, "muted"),)
            self._commit()

    def ai_repairing(self, lane: Lane, reason: str) -> None:
        """AI 的回答不合法、正在改正：快速、选英雄、选项的 AI 区写"改正中…"（原因只在终端里）。
        深度分析不改任何状态，面板上还是"深度分析中…"。"""
        if lane == "deep":
            return
        repairing = (Line(REPAIRING, "muted"),)
        with self._lock:
            if lane == "hero":
                self._hero_ai = repairing
            elif lane == "choice":
                self._choice_ai = repairing
            else:
                self._ai = repairing
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

    # ---- 选项 ----

    def show_choice(self, state: GameState, advice: ChoiceAdvice | None) -> None:
        """advice 为 None：游戏刚弹出一组选项（"选择"区由 on_update 的状态画出来）。"""
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
            self._rules = ()
            self._ai = ()
            self._deep = None
            self._deep_pending = False
            self._hero_rules = None
            self._hero_ai = ()
            self._choice_ai = ()

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
        choice = state.choice
        if choice is not None and isinstance(self._choice_ai, tuple) and self._choice_ai:
            # 选项期间 AI 区写选项的"分析中…"或错误提示（和选英雄一样）
            ai, ai_title = self._choice_ai, AI_TITLE
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
            next_step=self._choice_step(choice) if choice else self._recruit_step(state),
            choice=self._choice_lines(choice) if choice else (),
        )

    # ---- 选项 ----

    def _choice_advice(self, choice: Choice) -> ChoiceAdvice | None:
        entry = self._choice_ai
        if isinstance(entry, _ChoiceEntry) and entry.advice.choice_id == choice.id:
            return entry.advice
        return None

    def _choice_lines(self, choice: Choice) -> tuple[Line, ...]:
        """选择区：第一行"小型饰品 · 4 选 1"，下一行按顺序列出选项名，★ 标在 AI 推荐的前面。"""
        advice = self._choice_advice(choice)
        star = advice.pick if advice is not None else None
        names = " · ".join(
            ("★" if o.entity_id == star else "") + self._name(o.card_id) for o in choice.options
        )
        title = f"{choice_title(choice, self._name)} · {len(choice.options)} 选 1"
        return (Line(title), Line(names, role="comment"))

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
        failed = isinstance(self._choice_ai, tuple) and any(
            line.text not in (PENDING, REPAIRING) for line in self._choice_ai
        )
        if failed or not self._choice_enabled or self._ai_off is not None or advice is not None:
            return NextStep(SELF_CHOICE, tone="muted")
        return NextStep(WAIT_CHOICE, tone="muted")

    # ---- 招募 ----

    def _current_plan(self, state: GameState) -> tuple[_PlanEntry | None, bool]:
        """面板上现在显示的方案：本回合的深度分析（没过期、商店没刷新）→ 快速建议 → 规则。
        第二个值为 True 表示快速建议的商店刷新了、正在等新的 AI 建议。"""
        deep = self._deep
        if deep is not None and not deep.expired and not _shop_refreshed(deep.base, state):
            return deep, False
        fast = self._ai
        if isinstance(fast, _PlanEntry):
            if not _shop_refreshed(fast.base, state):
                return fast, False
            if self._ai_enabled and self._ai_off is None:
                return None, True
        rules = self._rules
        return (rules if isinstance(rules, _PlanEntry) else None), False

    def _recruit_step(self, state: GameState) -> NextStep:
        entry, waiting = self._current_plan(state)
        if waiting:
            return NextStep(WAIT_NEW, tone="muted")
        if entry is None:
            return NextStep(WAIT, tone="muted")
        if not entry.plan.actions:
            return NextStep(NOTHING_TO_DO, tone="muted")
        plan = self._remaining(entry, state)
        if not plan.actions:
            return NextStep(ALL_DONE, tone="muted")
        first = plan.actions[0]
        # 高亮框上的编号是它在完整方案里的序号（和面板、终端里的 ①② 对应）
        number = next((i for i, a in enumerate(entry.plan.actions) if a is first), None)
        if number is None:
            number = entry.plan.actions.index(first) if first in entry.plan.actions else 0
        return self._action_step(first, state, str(number + 1))

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
            next_step=self._hero_step(select, star),
        )

    def _hero_step(self, select: HeroSelect, rules_pick: int | None) -> NextStep:
        """AI 的推荐优先，没有时用规则的 ★。"""
        entry = self._hero_ai
        pick = rules_pick
        if isinstance(entry, _HeroEntry) and entry.advice.signature == select.signature():
            pick = entry.advice.pick if entry.advice.pick is not None else rules_pick
        offered = select.offered
        index = next((i for i, h in enumerate(offered) if h.entity_id == pick), None)
        if index is None:
            return NextStep(WAIT, tone="muted")
        count = len(offered)
        return NextStep(
            text=f"{POINT}选 左数第 {index + 1} 个 {self._name(offered[index].card_id)}",
            target=Target(row="hero", index=index, count=count),
            slots=slots_text("hero", index, count),
            badge=CHOICE_BADGE,
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
