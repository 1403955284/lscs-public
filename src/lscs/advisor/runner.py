"""把规则建议（以及 AI 建议）接到 Pipeline 的状态更新上。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from lscs.advisor.context import build_context, hero_power_used
from lscs.advisor.display import AdviceSink
from lscs.advisor.hero_rules import rule_hero_pick
from lscs.advisor.models import Action, Advice, HeroAdvice, Lane, Plan
from lscs.advisor.rules import rule_plans
from lscs.advisor.rules_config import RulesConfig
from lscs.advisor.validate import validate_plan
from lscs.llm.notices import TIMEOUT
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
        EventType.HERO_POWER_CHANGED,  # 选项到了（技能能不能用）、用过了技能
    }
)
CHOICE_EVENTS = frozenset({EventType.CHOICE_OFFERED, EventType.CHOICE_RESOLVED})


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
        case "hero_power":
            hp = me_new.hero_power
            return hp is not None and not hero_power_used(hp)
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
        self.last_hero: HeroAdvice | None = None  # 最近一次选英雄的规则建议

    def on_update(self, state: GameState, events: list[Event]) -> None:
        self._state = state
        types = {e.type for e in events}
        if EventType.GAME_STARTED in types:
            self._rules_error_turn = None
            self.last_hero = None
            if self._scheduler is not None:
                self._scheduler.reset_game()
        if state.phase == Phase.HERO_SELECT and EventType.HEROES_OFFERED in types:
            # 只在出现一组新的备选英雄时提示（开局、重随）；之后的更新只交给调度器
            self._advise_hero(state)
        if state.phase == Phase.RECRUIT and state.me is not None:
            if types & CHOICE_EVENTS:
                # 回合开始的技能升级、饰品在 MAIN_ACTION 之前就弹出：不管招募就绪没有都处理
                self._advise_choice(state, types, events)
            # 回合开始时先发铸币（商店还是空的）、再摆商店，还可能先选饰品花掉铸币；
            # 这些都在 STEP 变成 MAIN_ACTION（招募开始）之前，不提示，也不通知调度器。
            # 重连时 STEP 直接以初始值出现、没有招募开始事件，所以看 main_action 而不是事件。
            ready = state.main_action or EventType.RECRUIT_STARTED in types
            if ready and types & TRIGGERS:
                self._advise(state, events)
        elif self._scheduler is not None:
            self._scheduler.observe(state, events, None)
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
            plans = []  # 规则出错时 AI 照常调用（提示词里本来就不附规则建议）
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
            self._scheduler.observe(state, events, ctx)

    def _advise_choice(self, state: GameState, types: set[EventType], events: list[Event]) -> None:
        """游戏弹出了一组新的选项：先交给显示端列出来（面板），AI 的推荐稍后由调度器给出；
        选完了：调度器作废还没回来的请求。没有规则版本（拿不准就不乱猜）。"""
        if EventType.CHOICE_OFFERED in types and state.choice is not None:
            self._display.show_choice(state, None)
        if self._scheduler is not None:
            self._scheduler.observe_choice(state, events)

    def _advise_hero(self, state: GameState) -> None:
        select = state.hero_select
        if select is None:
            return
        try:
            advice = rule_hero_pick(select)
        except Exception:
            self._rules_error(state.turn, hero=True)  # AI 照常调用
            return
        self.last_hero = advice
        self._display.show_hero(state, advice)

    def _rules_error(self, turn: int, hero: bool = False) -> None:
        """在 except 里调用：每回合只提示一次、记一次完整堆栈，之后的只记调试日志。"""
        if self._rules_error_turn == turn:
            log.debug("计算规则建议时又出错", exc_info=True)
            return
        self._rules_error_turn = turn
        log.warning("计算规则建议时出错", exc_info=True)
        self._display.rules_error(hero)

    def tick(self) -> None:
        """主循环每次轮询调用：取回 AI 的结果并显示。"""
        if self._scheduler is None:
            return
        from lscs.llm.scheduler import AINotice, AIResult, AIStarted

        try:
            for event in self._scheduler.tick():
                if isinstance(event, AIStarted):
                    self._display.ai_pending(event.lane)
                elif isinstance(event, AINotice):
                    if event.lane is None:
                        self._display.ai_text(event.text)
                    else:
                        self._display.ai_text(event.text, event.lane)
                elif isinstance(event, AIResult):
                    self._show_result(event)
        except Exception:
            # 这里在 watch 主循环里运行，出错只记日志，不能让 watch 退出
            log.warning("处理 AI 结果时出错", exc_info=True)
            self._display.ai_text("AI 结果处理出错，本次忽略")

    def _show_result(self, result: AIResult) -> None:
        lane = result.lane
        if lane == "hero":
            self._show_hero_result(result)
            return
        if lane == "choice":
            self._show_choice_result(result)
            return
        if result.error is not None:
            self._display.ai_text(_error_text(result.error, lane), lane)
            return
        request, current = result.request, self._state
        if current is None or current.me is None or current.turn != request.state.turn:
            log.info("AI 结果回来时已不在同一个回合，丢弃")
            return
        plan, stale, expired = result.plan, None, result.expired
        if current.phase != Phase.RECRUIT:
            # 深度分析可能在本回合的战斗中才回来：操作做不了了，但思路下回合还用得上
            if lane == "fast" or current.phase != Phase.COMBAT:
                log.info("AI 结果回来时已不在招募阶段，丢弃")
                return
            expired = "changed"
        if expired is None and current.seq != request.state.seq:
            # 深度分析的操作用不上时（做完了或局面变化太大）照样显示，只看思路
            plan = remaining_plan(plan, request.state, current)
            if plan.actions == () and result.plan.actions:
                if lane == "fast":
                    return  # 建议的步骤你都已经做了
                expired = "changed"
            else:
                ctx = build_context(current, self._rules.buy_cost_for(current.me.hero_card_id))
                if ctx is None or validate_plan(plan.actions, ctx) is not None:
                    if lane == "fast":
                        self._display.ai_text("AI 建议已过期（局面变化太大）")
                        return
                    expired = "changed"
                else:
                    stale = f"基于 {self._clock() - request.started:.0f} 秒前的局面"
        if expired is not None:
            plan = result.plan
        agrees = (
            expired is None
            and self.last_rules is not None
            and self.last_rules.turn == current.turn
            and self.last_rules.plan.signature() == plan.signature()
        )
        advice = Advice(
            state_seq=request.state.seq,
            turn=request.state.turn,
            plan=plan,
            stale_note=stale,
            elapsed=result.elapsed,
            lane=lane,
            expired=expired,
        )
        self._display.show_ai(current, advice, agrees)

    def _show_hero_result(self, result: AIResult) -> None:
        """选英雄的 AI 结果：已经选完（或已经过了选英雄阶段）、重随过（备选变了）都丢弃，
        只记日志——新的请求已经发出，面板上的"分析中…"是它的。"""
        if result.error is not None:
            self._display.ai_text(result.error, "hero")
            return
        current, hero = self._state, result.hero
        select = current.hero_select if current is not None else None
        if current is None or current.phase != Phase.HERO_SELECT or select is None:
            log.info("选英雄的 AI 结果回来时已经选完英雄，丢弃")
            return
        if hero is None or select.signature() != result.request.signature:
            log.info("选英雄的 AI 结果回来时备选英雄已经变了，丢弃")
            return
        advice = hero.model_copy(update={"elapsed": result.elapsed})
        rules = self.last_hero
        agrees = (
            rules is not None
            and rules.signature == advice.signature
            and rules.pick is not None
            and rules.pick == advice.pick
        )
        self._display.show_hero(current, advice, agrees)

    def _show_choice_result(self, result: AIResult) -> None:
        """选项的 AI 结果（或错误）：那组已经选完、换了一组都丢弃，只记日志。"""
        current = self._state
        choice = current.choice if current is not None else None
        if current is None or choice is None or choice.id != result.request.choice_id:
            log.info("选项的 AI 结果回来时已经选完，丢弃")
            return
        if result.error is not None:
            self._display.ai_text(_error_text(result.error, "choice"), "choice")
            return
        if result.choice is None:
            return
        advice = result.choice.model_copy(update={"elapsed": result.elapsed})
        self._display.show_choice(current, advice)


def _error_text(error: str, lane: Lane) -> str:
    if lane != "deep":
        return error
    return "深度分析超时" if error == TIMEOUT else f"深度分析失败：{error}"
