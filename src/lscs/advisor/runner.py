"""把规则建议（以及 AI 建议）接到 Pipeline 的状态更新上。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from lscs.advisor.context import MINION, TurnContext, build_context, hero_power_used
from lscs.advisor.display import AdviceSink
from lscs.advisor.hero_rules import rule_hero_pick
from lscs.advisor.models import Action, Advice, HeroAdvice, Lane, Plan, PositionAdvice, StatsNote
from lscs.advisor.opponents import summarize_opponents
from lscs.advisor.pool import PoolEstimator, PoolNote, PoolView, pool_notes
from lscs.advisor.positioning import plan_positions, validate_position
from lscs.advisor.rules import rule_plans
from lscs.advisor.rules_config import RulesConfig
from lscs.advisor.stats_note import hero_note, trinket_note
from lscs.advisor.validate import validate_plan
from lscs.llm.notices import TIMEOUT
from lscs.state.models import ChoiceKind, Event, EventType, GameState, Phase

if TYPE_CHECKING:
    from lscs.cards.db import CardDB
    from lscs.llm.scheduler import AIResult, LLMScheduler
    from lscs.stats.model import MetaStats

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


def bought_everything(ctx: TurnContext, plans: list[Plan]) -> bool:
    """规则方案里的买、卖都做完了（站位建议设计文档第 5.1 节）：最优方案里没有买也没有卖；
    剩下刷新、英雄技能、升本不算，它们不改变场面。规则出错（没有方案）时看铸币还够不够买一张。
    手牌里有没有随从不管：打出去后场面变了会重算。"""
    if plans:
        return not any(a.type in ("buy", "sell") for a in plans[0].actions)
    return ctx.gold < ctx.buy_cost


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
        pool: PoolEstimator | None = None,
        cards: CardDB | None = None,
        positioning: bool = True,
        stats: Callable[[], MetaStats | None] | None = None,
    ) -> None:
        """pool：查过卡牌库的卡池估计；没有卡牌数据时为 None，"还剩约 N 张"都不显示。
        cards：卡牌数据，站位建议要看卡牌说明（亡语、进击、复仇、劈砍）；为 None 时不提示站位。
        positioning：config.toml 的 [positioning] enabled。
        stats：取本版本统计（StatsStore.get，还没准备好时返回 None）；统计关闭时为 None。"""
        self._rules = rules
        self._stats = stats
        self._name = name
        self._display = display
        self._scheduler = scheduler
        self._clock = clock
        self._pool = pool
        self._pool_failed = False  # 卡池估计出过错（完整堆栈只记一次）
        self._cards = cards if positioning else None  # 不提示站位时为 None
        self._position_intro = False  # 本局已经打过站位的机制说明
        self._position_failed = False  # 计算站位出过错（完整堆栈只记一次）
        self._state: GameState | None = None
        self._rules_error_turn: int | None = None  # 这个回合已经提示过规则出错
        self._opponents_turn: int | None = None  # 这个回合已经列过对手一览
        self.last_rules: Advice | None = None
        self.last_hero: HeroAdvice | None = None  # 最近一次选英雄的规则建议

    def on_update(self, state: GameState, events: list[Event]) -> None:
        self._state = state
        types = {e.type for e in events}
        if EventType.GAME_STARTED in types:
            self._rules_error_turn = None
            self._opponents_turn = None
            self._position_intro = False
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

    def _pool_error(self, what: str) -> None:
        """在 except 里调用：卡池估计出错不影响建议，只是不显示张数；完整堆栈只记一次。"""
        if self._pool_failed:
            log.debug("%s时又出错", what, exc_info=True)
            return
        self._pool_failed = True
        log.warning("%s时出错", what, exc_info=True)

    def _pool_view(self, state: GameState) -> PoolView | None:
        if self._pool is None:
            return None
        try:
            return self._pool.view(state)
        except Exception:
            self._pool_error("估计卡池")
            return None

    def _context(self, state: GameState) -> TurnContext | None:
        buy_cost = self._rules.buy_cost_for(state.me.hero_card_id)
        return build_context(state, buy_cost, pool=self._pool_view(state))

    def _notes(
        self, state: GameState, ctx: TurnContext | None, look_for: tuple[str, ...] = ()
    ) -> tuple[PoolNote, ...]:
        """对子和要找的牌的张数（Advice.pool_notes）；没有卡池估计时为空。"""
        if ctx is None or ctx.pool is None:
            return ()
        shop = [s.card_id for s in state.shop if s.card_type == MINION]
        try:
            return pool_notes(ctx.pool, ctx, shop, look_for, self._name)
        except Exception:
            self._pool_error("计算对子和要找的牌的张数")
            return ()

    def _show_opponents(self, state: GameState) -> None:
        """每回合第一次给建议之前列一次对手一览（招募开始，或者重连后第一次看到这个回合）。
        出错只记日志，建议照常。"""
        if self._opponents_turn == state.turn:
            return
        self._opponents_turn = state.turn
        if not state.opponents:
            return
        try:
            overview = summarize_opponents(state)
        except Exception:
            log.warning("整理对手一览时出错", exc_info=True)
            return
        self._display.show_opponents(state, overview)

    def _advise(self, state: GameState, events: list[Event]) -> None:
        self._show_opponents(state)
        try:
            ctx = self._context(state)
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
                    pool_notes=self._notes(state, ctx),
                )
                self.last_rules = advice
                self._display.show_rules(state, advice)
        if self._scheduler is not None:
            self._scheduler.observe(state, events, ctx)
        settled = bought_everything(ctx, plans)
        self._display.show_position(state, self._position(state) if settled else None)

    def _position(self, state: GameState) -> PositionAdvice | None:
        """买完牌后的站位建议（规则，不等 AI）；没有卡牌数据、功能关闭、少于 2 个随从时为 None。
        算错、不合法只记日志，不显示。本局第一次有要拖的步时带上机制说明（intro）。"""
        cards, me = self._cards, state.me
        if cards is None or me is None:
            return None
        try:
            advice = plan_positions(state, cards, self._name)
            if advice is None:
                return None
            problem = validate_position(advice, tuple(m.entity_id for m in me.board))
        except Exception:
            if self._position_failed:
                log.debug("计算站位建议时又出错", exc_info=True)
            else:
                self._position_failed = True
                log.warning("计算站位建议时出错", exc_info=True)
            return None
        if problem is not None:
            log.warning("站位建议不合法（%s），不显示", problem)
            return None
        if advice.moves and not self._position_intro:
            self._position_intro = True
            advice = advice.model_copy(update={"intro": True})
        return advice

    def _advise_choice(self, state: GameState, types: set[EventType], events: list[Event]) -> None:
        """游戏弹出了一组新的选项：先交给显示端列出来（面板），AI 的推荐稍后由调度器给出；
        选完了：调度器作废还没回来的请求。没有规则版本（拿不准就不乱猜）。"""
        choice = state.choice
        if EventType.CHOICE_OFFERED in types and choice is not None:
            if choice.kind == ChoiceKind.TRINKET:  # 其他种类的选项不出条目（随从的数字不显示）
                stats = self._current_stats()
                if stats is not None:
                    self._show_stats(state, lambda: trinket_note(choice, stats))
            self._display.show_choice(state, None)
        if self._scheduler is not None:
            self._scheduler.observe_choice(state, events)

    def _current_stats(self) -> MetaStats | None:
        """本版本统计：关闭、还没准备好、取的时候出错都是 None（出错只记日志）。"""
        if self._stats is None:
            return None
        try:
            return self._stats()
        except Exception:
            log.warning("取本版本统计时出错", exc_info=True)
            return None

    def _show_stats(self, state: GameState, make: Callable[[], StatsNote | None]) -> None:
        """整理并显示本版本统计的条目；出错只记日志，建议照常。"""
        try:
            note = make()
        except Exception:
            log.warning("整理本版本统计时出错", exc_info=True)
            return
        if note is not None:
            self._display.show_stats(state, note)

    def _advise_hero(self, state: GameState) -> None:
        select = state.hero_select
        if select is None:
            return
        # 统计在这时还没准备好（watch 刚启动）就按没有统计处理，之后不回头重画
        stats = self._current_stats()
        if stats is not None:
            self._show_stats(state, lambda: hero_note(select, stats))
        try:
            advice = rule_hero_pick(select, stats)
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
        from lscs.llm.scheduler import AINotice, AIRepairing, AIResult, AIStarted

        try:
            for event in self._scheduler.tick():
                if isinstance(event, AIStarted):
                    self._display.ai_pending(event.lane)
                elif isinstance(event, AIRepairing):
                    self._display.ai_repairing(event.lane, event.reason)
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
        # 错误（超时、调用失败）和方案一样先看还在不在那个局面：回合已经过去了就只记日志
        request, current = result.request, self._state
        if current is None or current.me is None or current.turn != request.state.turn:
            log.info("AI 结果回来时已不在同一个回合，丢弃")
            return
        if current.phase != Phase.RECRUIT:
            # 深度分析可能在本回合的战斗中才回来：操作做不了了，但思路下回合还用得上
            if lane == "fast" or current.phase != Phase.COMBAT:
                log.info("AI 结果回来时已不在招募阶段，丢弃")
                return
        if result.error is not None:
            self._display.ai_text(_error_text(result.error, lane), lane)
            return
        plan, stale = result.plan, None
        expired = result.expired if current.phase == Phase.RECRUIT else "changed"
        if expired is None and current.seq != request.state.seq:
            # 深度分析的操作用不上时（做完了或局面变化太大）照样显示，只看思路
            plan = remaining_plan(plan, request.state, current)
            if plan.actions == () and result.plan.actions:
                if lane == "fast":
                    return  # 建议的步骤你都已经做了
                expired = "changed"
            else:
                ctx = self._context(current)
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
        # 张数按现在的局面算（结果回来时可能已经买了一张）；深度分析在战斗中才回来时没有招募的
        # 局面，退回用发出请求时的局面
        basis, ctx = current, self._context(current)
        if ctx is None:
            basis, ctx = request.state, self._context(request.state)
        notes = self._notes(basis, ctx, plan.look_for)
        advice = Advice(
            state_seq=request.state.seq,
            turn=request.state.turn,
            plan=plan,
            stale_note=stale,
            elapsed=result.elapsed,
            lane=lane,
            expired=expired,
            pool_notes=notes,
        )
        self._display.show_ai(current, advice, agrees)

    def _show_hero_result(self, result: AIResult) -> None:
        """选英雄的 AI 结果（或错误）：已经选完（或已经过了选英雄阶段）、重随过（备选变了）都
        丢弃，只记日志——新的请求已经发出，面板上的"分析中…"是它的。"""
        current, hero = self._state, result.hero
        select = current.hero_select if current is not None else None
        if current is None or current.phase != Phase.HERO_SELECT or select is None:
            log.info("选英雄的 AI 结果回来时已经选完英雄，丢弃")
            return
        if select.signature() != result.request.signature:
            log.info("选英雄的 AI 结果回来时备选英雄已经变了，丢弃")
            return
        if result.error is not None:
            self._display.ai_text(result.error, "hero")
            return
        if hero is None:
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
