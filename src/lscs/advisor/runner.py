"""把规则建议（以及 AI 建议）接到 Pipeline 的状态更新上。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from lscs.advisor.context import build_context
from lscs.advisor.display import AdviceSink
from lscs.advisor.models import Action, Advice, Plan
from lscs.advisor.rules import rule_plans
from lscs.advisor.rules_config import RulesConfig
from lscs.advisor.validate import validate_plan
from lscs.state.models import Event, EventType, GameState, Phase

if TYPE_CHECKING:
    from lscs.llm.scheduler import AIResult, LLMScheduler

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
TRIGGERS = frozenset(
    {
        EventType.RECRUIT_STARTED,
        EventType.SHOP_CHANGED,
        EventType.GOLD_CHANGED,
        EventType.BOARD_CHANGED,
        EventType.HAND_CHANGED,
    }
)


def _still_pending(action: Action, old: GameState, new: GameState) -> bool:
    """这一步在 old 局面之后是否还没执行。"""
    me_old, me_new = old.me, new.me
    if me_old is None or me_new is None:
        return False
    new_shop = {s.entity_id: s for s in new.shop}
    match action.type:
        case "buy":
            return action.entity_id in new_shop
        case "sell":
            return any(m.entity_id == action.entity_id for m in me_new.board)
        case "upgrade":
            return me_new.tavern_tier == me_old.tavern_tier
        case "refresh":
            old_ids = {s.entity_id for s in old.shop}
            return not new_shop or bool(old_ids & set(new_shop))
        case _:
            return not (new.shop and all(s.frozen for s in new.shop))


def remaining_plan(plan: Plan, old: GameState, new: GameState) -> Plan:
    """去掉在 old 之后已经执行的步骤。"""
    kept = tuple(a for a in plan.actions if _still_pending(a, old, new))
    return plan.model_copy(update={"actions": kept})


class AdviceRunner:
    def __init__(
        self,
        rules: RulesConfig,
        name: Namer,
        display: AdviceSink,
        scheduler: LLMScheduler | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._rules = rules
        self._name = name
        self._display = display
        self._scheduler = scheduler
        self._clock = clock
        self._state: GameState | None = None
        self._rules_error_turn: int | None = None  # 这个回合已经提示过规则出错
        self.last_rules: Advice | None = None

    def on_update(self, state: GameState, events: list[Event]) -> None:
        self._state = state
        types = {e.type for e in events}
        if EventType.GAME_STARTED in types:
            self._rules_error_turn = None
            if self._scheduler is not None:
                self._scheduler.reset_game()
        if state.phase == Phase.RECRUIT and state.me is not None:
            # 回合开始时先发铸币（商店还是空的）、再摆商店，还可能先选饰品花掉铸币；
            # 这些都在 STEP 变成 MAIN_ACTION（招募开始）之前，不提示，也不通知调度器。
            # 重连时 STEP 直接以初始值出现、没有招募开始事件，所以看 main_action 而不是事件。
            ready = state.main_action or EventType.RECRUIT_STARTED in types
            if ready and types & TRIGGERS:
                self._advise(state, events)
        elif self._scheduler is not None:
            self._scheduler.observe(state, events, None, [])
        s = self._scheduler
        if s is not None and EventType.GAME_ENDED in types and s.calls > 0:
            self._display.ai_summary(s.calls, s.input_tokens, s.output_tokens)

    def _advise(self, state: GameState, events: list[Event]) -> None:
        try:
            ctx = build_context(state, self._rules.buy_cost_for(state.me.hero_card_id))
        except Exception:
            self._rules_error(state.turn)
            return
        if ctx is None:
            return
        try:
            plans = rule_plans(ctx, self._rules, self._name)
        except Exception:
            self._rules_error(state.turn)
            plans = []  # 规则出错时 AI 照常调用，只是提示词里没有规则建议
        if plans:
            started = any(e.type == EventType.RECRUIT_STARTED for e in events)
            if plans[0].actions or started:  # 回合中途铸币花光后不再提示"无需操作"
                advice = Advice(
                    state_seq=state.seq,
                    turn=state.turn,
                    plan=plans[0],
                    alternatives=tuple(plans[1:]),
                )
                self.last_rules = advice
                self._display.show_rules(state, advice)
        if self._scheduler is not None:
            self._scheduler.observe(state, events, ctx, plans)

    def _rules_error(self, turn: int) -> None:
        """在 except 里调用：每回合只提示一次、记一次完整堆栈，之后的只记调试日志。"""
        if self._rules_error_turn == turn:
            log.debug("计算规则建议时又出错", exc_info=True)
            return
        self._rules_error_turn = turn
        log.warning("计算规则建议时出错", exc_info=True)
        self._display.rules_error()

    def tick(self) -> None:
        """主循环每次轮询调用：取回 AI 的结果并显示。"""
        if self._scheduler is None:
            return
        from lscs.llm.scheduler import AINotice, AIResult, AIStarted

        try:
            for event in self._scheduler.tick():
                if isinstance(event, AIStarted):
                    self._display.ai_pending()
                elif isinstance(event, AINotice):
                    self._display.ai_text(event.text)
                elif isinstance(event, AIResult):
                    self._show_result(event)
        except Exception:
            # 这里在 watch 主循环里运行，出错只记日志，不能让 watch 退出
            log.warning("处理 AI 结果时出错", exc_info=True)
            self._display.ai_text("AI 结果处理出错，本次忽略")

    def _show_result(self, result: AIResult) -> None:
        if result.error is not None:
            self._display.ai_text(result.error)
            return
        request, current = result.request, self._state
        if (
            current is None
            or current.me is None
            or current.phase != Phase.RECRUIT
            or current.turn != request.state.turn
        ):
            log.info("AI 结果回来时已不在同一个招募回合，丢弃")
            return
        plan, stale = result.plan, None
        if current.seq != request.state.seq:
            plan = remaining_plan(plan, request.state, current)
            if plan.actions == () and result.plan.actions:
                return  # 建议的步骤你都已经做了
            ctx = build_context(current, self._rules.buy_cost_for(current.me.hero_card_id))
            if ctx is None or validate_plan(plan.actions, ctx) is not None:
                self._display.ai_text("AI 建议已过期（局面变化太大）")
                return
            stale = f"基于 {self._clock() - request.started:.0f} 秒前的局面"
        agrees = (
            self.last_rules is not None
            and self.last_rules.turn == current.turn
            and self.last_rules.plan.signature() == plan.signature()
        )
        advice = Advice(
            state_seq=request.state.seq,
            turn=request.state.turn,
            plan=plan,
            stale_note=stale,
            elapsed=result.elapsed,
        )
        self._display.show_ai(current, advice, agrees)
