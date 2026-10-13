"""决定何时调用大模型，并在后台执行（M2 设计文档第 7.4 节，更聪明的 AI 建议设计文档第 3.3 节，
选英雄建议设计文档第 6 节，选项建议设计文档第 4 节）。

四路调用，各自有当前请求、在途请求和超时；每局预算和认证失败计数四路共用：
- 快速（fast）：按 thinking / max_output_tokens / timeout_seconds 调用，2~3 秒出结果。
  招募开始时立即调用（重连、中途启动时没有招募开始事件：第一次看到这个回合也算，AI 自我改正
  设计文档第 5.2 节）；刷新（商店实体编号与上次完全不重合）后，商店稳定 debounce 秒再调用，
  期间的买卖会更新待发的局面；刷新前发出、还没返回的快速请求作废（方案里的牌已经不在商店了），
  进战斗、对局结束时也作废（操作做不了了）；返回空内容时重试一次。
- 深度（deep，deep_analysis 打开时）：按 deep_thinking / deep_max_output_tokens /
  deep_timeout_seconds 调用，约 20 秒。只在招募开始时和快速请求同时发出；不重试；
  刷新后不作废——操作过期，但分析仍然有用，结果照常交付并标明 expired="refreshed"。
- 选英雄（hero，hero_advice 打开时）：参数和快速建议一样，但用自己的系统提示和局面消息。
  出现一组新的备选英雄（开局、重随）后等 hero_debounce 秒（默认 0.5）再调用，上一组的请求作废：
  开局洗牌时 watch 可能读到半组英雄、紧接着又是完整的一组，等待期间来了新的一组就换成新的、
  重新计时，只调用一次（设计文档第 6.2 节，原设计是立即发出）。replay 同步执行时不等。
  返回空内容时重试一次。离开选英雄（选完、进招募）时待发和在途的请求都作废：
  选完之后再回来的推荐没有意义。
- 选项（choice，choice_advice 打开时）：参数和快速建议一样，用自己的系统提示和局面消息。
  游戏弹出一组新的"选一个"时立即调用（不等：三连奖励 3 秒就会选掉），上一组的请求作废；
  选完（或换组、进战斗、新对局）时待发和在途的请求都作废。不走 observe()：回合开始的技能
  升级和饰品在 MAIN_ACTION 之前弹出，由运行器看到选择事件就调用 observe_choice()。
  返回空内容时重试一次。结果不写进记忆（选项结果没有方向和要找的牌）。

其他规则：
- observe() 只记下待发的请求，由主循环每次轮询调用的 tick() 发出。watch 启动时会从头读
  Power.log，一次轮询里重放很多回合；这样只有读完后的最终局面才会真的调用（每路最多一次）；
- 每路同一时间只认最新的请求，旧请求的结果不显示；新回合、新对局时两路的旧请求都作废
  （例外：上一回合晚到的深度分析不显示，但它的方向和要找的牌会记下来）；
- 请求超过该路的超时 + 2 秒还没返回：提示"AI 超时"并作废；
- 作废了在途的请求、又因为预算用完或 AI 已关闭发不出新请求时，发一条分路的提示收尾，
  免得"分析中…"一直挂着（同上，第 5.1 节）；
- 每局最多 max_calls_per_game 次（四路合计，含空内容重试和改正）；
- Key 连续 3 次认证失败后关闭 AI；
- 记住本局最近一次合法结果的方向、要找的牌和分析（同一回合深度优先），附在之后的局面消息末尾；
- 回答解析不了或方案不合法时，把原因发回去让 AI 改正一次（AI 自我改正设计文档第 3 节，
  repair_invalid 打开时）：多轮对话，前缀和原请求相同；只在请求还是当前的、离截止时间还有
  REPAIR_MIN_SECONDS 秒、预算还有时才改正，占一次调用。深度这一路改正时不开思考。
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from lscs.advisor.context import TurnContext
from lscs.advisor.models import ChoiceAdvice, Expired, HeroAdvice, Lane, Plan
from lscs.advisor.validate import normalize_actions, validate_plan
from lscs.cards.db import CardDB
from lscs.config import LLMConfig
from lscs.llm.notices import (
    AI_OFF_THIS_TIME,
    AUTH_DISABLED,
    AUTH_FAILURE_LIMIT,
    BUDGET_EXHAUSTED,
    NO_BUDGET_THIS_TIME,
    TIMEOUT,
)
from lscs.llm.parse import (
    EmptyResponse,
    ParseError,
    parse_choice_pick,
    parse_hero_pick,
    parse_plan,
)
from lscs.llm.prompt import (
    AIMemory,
    build_choice_message,
    build_choice_system_prompt,
    build_hero_message,
    build_hero_system_prompt,
    build_opponents_block,
    build_system_prompt,
    build_user_message,
    repair_text,
)
from lscs.llm.provider import LLMError, LLMProvider, LLMResult, Thinking, Turn
from lscs.llm.record import Recorder
from lscs.state.models import Event, EventType, GameState, Phase

log = logging.getLogger(__name__)

# openai 的 timeout 是单次读取的超时，DeepSeek 的保活字节会让整次调用拖得更久；超过这个余量就放弃
DEADLINE_GRACE = 2.0
# 选英雄：看到一组备选英雄后等这么久再调用（开局洗牌时可能先读到半组英雄）
HERO_DEBOUNCE = 0.5
# 改正要发出时，离这一路的截止时间至少还要有这么多秒
REPAIR_MIN_SECONDS = 3.0
# 深度分析改正时用快速建议的输出上限，但至少这么多：深度分析的 json（分析写得长）不能被截断
DEEP_REPAIR_MIN_TOKENS = 4000
INVALID = "AI 建议无效，已忽略"
INVALID_AFTER_REPAIR = "AI 改正后仍不合法，已忽略"
EMPTY = "AI 返回了空内容"


@dataclass(frozen=True)
class AIRequest:
    request_id: int
    state: GameState
    ctx: TurnContext | None  # 选英雄没有
    started: float
    lane: Lane = "fast"
    signature: tuple[tuple[int, str], ...] = ()  # 选英雄：基于哪一组备选（HeroSelect.signature()）
    choice_id: int | None = None  # 选项：基于哪一组（Choice.id）


@dataclass(frozen=True)
class _Pending:
    state: GameState
    ctx: TurnContext | None  # 招募的两路总有；选英雄、选项没有
    due: float  # 到这个时间（clock）之后的第一次 tick() 发出
    rerolled: bool = False  # 选英雄：玩家重新随机过英雄


@dataclass(frozen=True)
class _Call:
    """后台线程里的一次调用：参数，以及调用记录要写的内容。"""

    sent: str  # 这次新增的 user 内容：原调用是局面消息，改正调用是改正要求
    thinking: Thinking
    max_tokens: int
    repair: bool = False  # 改正调用（以及改正的回答为空时的重试）


@dataclass(frozen=True)
class AIStarted:
    request_id: int
    lane: Lane = "fast"


@dataclass(frozen=True)
class AIRepairing:
    """AI 的回答不合法，正在让它改正（终端和面板提示，免得像卡住了）。"""

    request_id: int
    lane: Lane
    reason: str  # 校验器或解析器的原因


@dataclass(frozen=True)
class AINotice:
    text: str
    lane: Lane | None = None  # 只属于某一路的提示（选项的局面出错）；None 表示不分路


@dataclass(frozen=True)
class AIResult:
    request: AIRequest
    plan: Plan | None
    error: str | None
    elapsed: float
    llm: LLMResult | None = None
    # 只有深度分析会有：plan 里的操作不能用，只看分析（refreshed 发出后商店刷新过；invalid 不合法）
    expired: Expired | None = None
    hero: HeroAdvice | None = None  # 选英雄的结果（plan 为 None）
    choice: ChoiceAdvice | None = None  # 选项的结果（plan 为 None）
    repaired: bool = False  # 这个请求让 AI 改正过一次

    @property
    def lane(self) -> Lane:
        return self.request.lane


SchedulerEvent = AIStarted | AIRepairing | AINotice | AIResult


@dataclass
class _LaneState:
    """一路调用的设置和状态。请求编号四路共用一个计数器（调用记录里不重复），
    但每路只认自己最新的请求。

    线程：所有状态字段只在主线程（observe / tick / reset_game）里写。后台线程只读设置
    （thinking 等，创建后不变），以及在重试、改正前通过 is_current() 读 current_id、stale_up_to；
    所以这两个字段的写入和这次读取都持有调度器的锁。refreshed_up_to、pending、in_flight
    只在主线程读写，不加锁。"""

    name: Lane
    thinking: Thinking
    max_tokens: int
    timeout: float
    retry_empty: bool
    current_id: int = 0
    stale_up_to: int = 0  # 编号不大于它的请求作废
    refreshed_up_to: int = 0  # 编号不大于它的请求发出后商店刷新过（只对深度有意义）
    pending: _Pending | None = None
    # 最近发出、还没向用户交代的请求（显示了"分析中…"）。作废后也保留，直到有新请求发出，
    # 或者发不出新请求时由 _settle 收尾（设计文档第 5.1 节）
    in_flight: AIRequest | None = None

    def is_current(self, request: AIRequest) -> bool:
        return self.is_current_id(request.request_id)

    def is_current_id(self, request_id: int) -> bool:
        return self.stale_up_to < request_id == self.current_id


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="lscs-llm", daemon=True).start()


class LLMScheduler:
    def __init__(
        self,
        provider: LLMProvider,
        cfg: LLMConfig,
        cards: CardDB,
        guide: str,
        heroes_note: str | None = None,
        recorder: Recorder | None = None,
        debounce: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        run_async: Callable[[Callable[[], None]], None] = _run_in_thread,
        hero_debounce: float = HERO_DEBOUNCE,
    ) -> None:
        self._provider = provider
        self._cfg = cfg
        self._cards = cards
        self._system = build_system_prompt(guide)
        self._system_hash = _sha1(self._system)
        self._hero_system = build_hero_system_prompt(guide, heroes_note)
        self._hero_hash = _sha1(self._hero_system)
        self._choice_system = build_choice_system_prompt(guide)
        self._choice_hash = _sha1(self._choice_system)
        self._recorder = recorder
        self._debounce = debounce
        self._hero_debounce = hero_debounce
        self._clock = clock
        self._run_async = run_async
        self._events: queue.Queue[SchedulerEvent] = queue.Queue()
        self._lock = threading.Lock()
        self._fast = _LaneState(
            "fast", cfg.thinking, cfg.max_output_tokens, cfg.timeout_seconds, retry_empty=True
        )
        self._deep = _LaneState(
            "deep",
            cfg.deep_thinking,
            cfg.deep_max_output_tokens,
            cfg.deep_timeout_seconds,
            retry_empty=False,
        )
        self._hero = _LaneState(
            "hero", cfg.thinking, cfg.max_output_tokens, cfg.timeout_seconds, retry_empty=True
        )
        self._choice = _LaneState(
            "choice", cfg.thinking, cfg.max_output_tokens, cfg.timeout_seconds, retry_empty=True
        )
        self._recruit_lanes = (self._fast, self._deep)
        # 同时到期时先发快速
        self._lanes = (self._fast, self._deep, self._hero, self._choice)
        self._last_id = 0
        self._auth_failures = 0
        self.disabled_reason: str | None = None
        self.reset_game()

    @property
    def memory(self) -> AIMemory | None:
        return self._memory

    @property
    def provider(self) -> LLMProvider:
        """对局复盘用同一个提供商和配置（不占本局的调用预算）。"""
        return self._provider

    @property
    def config(self) -> LLMConfig:
        return self._cfg

    @property
    def recorder(self) -> Recorder | None:
        return self._recorder

    @property
    def deep_thinking(self) -> str:
        return self._cfg.deep_thinking

    @property
    def choice_advice(self) -> bool:
        return self._cfg.choice_advice

    def reset_game(self) -> None:
        # 上一局还没返回的请求全部作废
        with self._lock:
            for lane in self._lanes:
                lane.stale_up_to = lane.current_id
            self.calls = 0
            self.input_tokens = 0
            self.output_tokens = 0
        for lane in self._lanes:
            lane.pending = None
            lane.in_flight = None
        self._game_first_id = self._last_id  # 编号不大于它的请求属于以前的对局
        self._budget_notified = False
        self._memory: AIMemory | None = None
        self._last_shop: frozenset[int] = frozenset()
        self._last_turn: int | None = None
        # (酒馆回合, 对手块)：本回合第一次生成招募消息时算好，整回合复用（对手在招募中途升本、
        # 加护甲也不改），同一回合的几次调用都能命中这一段前缀缓存
        self._opponents: tuple[int, str] | None = None

    def observe(self, state: GameState, events: Sequence[Event], ctx: TurnContext | None) -> None:
        """每次状态更新都调用；只记下要发的请求，不在这里发出。"""
        self._observe_hero(state, events)
        if state.phase != Phase.RECRUIT:
            # 进战斗、对局结束：快速建议的操作做不了了，在途的请求作废（不再为它改正，结果只记
            # 日志）；深度分析的思路在战斗里照样交付；还没选的那组选项不会再选了
            self._deep.pending = None
            self._drop(self._fast)
            self._drop(self._choice)
            return
        shop = frozenset(s.entity_id for s in state.shop)
        refreshed = (
            bool(shop)
            and bool(self._last_shop)
            and not (shop & self._last_shop)
            and state.turn == self._last_turn
        )
        # 重连、中途启动时没有招募开始事件（STEP 直接以 MAIN_ACTION 出现）：这一路第一次看到
        # 这个回合也当作招募开始（设计文档第 5.2 节）
        started = (
            any(e.type == EventType.RECRUIT_STARTED for e in events)
            or state.turn != self._last_turn
        )
        if started:
            with self._lock:
                for lane in self._recruit_lanes:
                    lane.stale_up_to = lane.current_id  # 新回合：上回合两路的请求都作废
        elif refreshed:
            with self._lock:
                self._fast.stale_up_to = self._fast.current_id  # 刷新前的快速请求作废（7.5 节）
            self._deep.refreshed_up_to = self._deep.current_id  # 深度分析只是操作过期
        now = self._clock()
        if ctx is None:
            pass  # 没有 ctx 的局面不能生成请求；保留之前待发的请求
        elif started:
            self._fast.pending = _Pending(state, ctx, now)
            if self._cfg.deep_analysis:
                self._deep.pending = _Pending(state, ctx, now)
        elif refreshed:
            self._fast.pending = _Pending(state, ctx, now + self._debounce)
            if self._deep.pending is not None:  # 深度分析还没发出：改用刷新后的商店
                self._deep.pending = replace(self._deep.pending, state=state, ctx=ctx)
        else:
            # 还没发出请求时又买卖了：用最新的局面，时间不变
            for lane in self._recruit_lanes:
                if lane.pending is not None:
                    lane.pending = replace(lane.pending, state=state, ctx=ctx)
        if shop or state.turn != self._last_turn:
            self._last_shop = shop  # 买空商店时保留上次的商品，之后刷新仍能识别
        self._last_turn = state.turn

    def _observe_hero(self, state: GameState, events: Sequence[Event]) -> None:
        hero = self._hero
        if state.phase == Phase.HERO_SELECT and state.hero_select is not None:
            offers = [e for e in events if e.type == EventType.HEROES_OFFERED]
            if not offers:
                return  # 已经在等这一组的结果了
            with self._lock:
                hero.stale_up_to = hero.current_id  # 重随：上一组的请求作废
            replaced, hero.pending = hero.pending, None
            if self._cfg.hero_advice:
                # 替换还没发出的那组（洗牌读到一半）时沿用它的"是否重随"，免得说成重新随机
                rerolled = (
                    replaced.rerolled
                    if replaced is not None
                    else bool(offers[-1].data.get("changed"))
                )
                due = self._clock() + self._hero_debounce
                hero.pending = _Pending(state, None, due, rerolled)
            return
        self._drop(hero)

    def observe_choice(self, state: GameState, events: Sequence[Event]) -> None:
        """有选择事件（CHOICE_OFFERED / CHOICE_RESOLVED）的状态更新都调用；只记下要发的请求。
        新的一组：上一组的请求作废，这一组立即到期。选完了：待发和在途的请求都作废。"""
        lane = self._choice
        offered = any(e.type == EventType.CHOICE_OFFERED for e in events)
        if offered and state.choice is not None:
            self._drop(lane)
            if self._cfg.choice_advice:
                lane.pending = _Pending(state, None, self._clock())
            return
        if state.choice is None or any(e.type == EventType.CHOICE_RESOLVED for e in events):
            self._drop(lane)

    def _drop(self, lane: _LaneState) -> None:
        """待发的请求不发了，在途的请求作废（结果回来时只记日志）。"""
        lane.pending = None
        if lane.current_id > lane.stale_up_to:
            with self._lock:
                lane.stale_up_to = lane.current_id

    def tick(self) -> list[SchedulerEvent]:
        """主循环每次轮询调用：取回已完成的结果，发出到期的请求，处理超时。"""
        out: list[SchedulerEvent] = []
        self._drain(out)  # 先取回结果、更新记忆：这次发出的请求就能用上最新的判断
        self._start_due()
        self._drain(out)  # 同步执行时（replay）刚发出的请求已经有结果了
        for lane in self._lanes:
            request = lane.in_flight
            if request is None:
                continue
            waited = self._clock() - request.started
            if not lane.is_current(request):
                continue  # 已作废：结果回来时只记日志，不算超时
            if waited > lane.timeout + DEADLINE_GRACE:
                log.info("AI 请求 #%s 等了 %.1f 秒还没返回，放弃", request.request_id, waited)
                with self._lock:
                    lane.stale_up_to = request.request_id
                lane.in_flight = None
                out.append(AIResult(request, None, TIMEOUT, waited))
        return out

    def _drain(self, out: list[SchedulerEvent]) -> None:
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                return
            if isinstance(event, AIRepairing):
                if not self._lane_named(event.lane).is_current_id(event.request_id):
                    log.info("丢弃已被取代的请求 #%s 的改正提示", event.request_id)
                    continue
            elif isinstance(event, AIResult):
                lane = self._lane(event.request)
                if not lane.is_current(event.request):
                    log.info("丢弃已被取代的 AI 结果 #%s", event.request.request_id)
                    self._remember_late(event)
                    continue
                lane.in_flight = None
                if (
                    event.plan is not None
                    and event.expired is None
                    and event.request.request_id <= lane.refreshed_up_to
                ):
                    event = replace(event, expired="refreshed")
                self._remember(event)
            out.append(event)

    def _lane(self, request: AIRequest) -> _LaneState:
        return self._lane_named(request.lane)

    def _lane_named(self, name: Lane) -> _LaneState:
        return next(lane for lane in self._lanes if lane.name == name)

    def _remember_late(self, result: AIResult) -> None:
        """本局上一回合的深度分析在新回合开始后才返回：不显示，但它的判断仍然记下来。"""
        request = result.request
        if (
            result.lane == "deep"
            and request.request_id > self._game_first_id
            and self._last_turn is not None
            and request.state.turn == self._last_turn - 1
        ):
            self._remember(result)

    def _remember(self, result: AIResult) -> None:
        """合法结果（深度分析操作过期也算）里有方向、要找或分析时记下来。
        按返回的先后，记最近的一次；但同一回合的深度分析不被快速建议覆盖，
        晚到的旧回合结果也不覆盖新回合的深度分析。"""
        plan = result.plan
        if plan is None or result.expired == "invalid":
            return
        if not (plan.direction or plan.look_for or plan.analysis):
            return
        turn, deep = result.request.state.turn, result.lane == "deep"
        old = self._memory
        if old is not None and old.deep and (old.turn > turn or (old.turn == turn and not deep)):
            return
        self._memory = AIMemory(turn, deep, plan.direction, plan.look_for, plan.analysis)

    def _start_due(self) -> None:
        now = self._clock()
        due = [(lane, lane.pending) for lane in self._lanes if lane.pending is not None]
        due = [(lane, p) for lane, p in due if now >= p.due]
        if not due:
            return
        for lane, _ in due:
            lane.pending = None
        if self.disabled_reason is not None:
            for lane, _ in due:
                self._settle(lane, AI_OFF_THIS_TIME)
            return
        budget_notified = self._budget_notified
        # 快速和深度同时发出时局面相同，只生成一次
        messages: dict[tuple[str, int], str | None] = {}
        for lane, pending in due:
            kind = "recruit" if lane in self._recruit_lanes else lane.name
            key = (kind, id(pending.state))
            if key not in messages:
                messages[key] = self._message(lane, pending)
            user = messages[key]
            if user is None or self._start(lane, pending, user):
                continue
            # 预算用完：这次刚发了"预算已用完"（不分路）时不再追加一行，只收尾
            self._settle(lane, NO_BUDGET_THIS_TIME if budget_notified else None)

    def _settle(self, lane: _LaneState, text: str | None) -> None:
        """这一路发不出新请求。还挂着一个已作废、没向用户交代过的请求时（刷新作废了在途的
        请求），发一条分路的提示收尾，否则"分析中…"永远不结束（设计文档第 5.1 节）。"""
        request = lane.in_flight
        if request is None or lane.is_current(request):
            return
        lane.in_flight = None
        if text is not None:
            self._events.put(AINotice(text, lane.name))

    def _message(self, lane: _LaneState, pending: _Pending) -> str | None:
        # 先生成局面消息：出错时不占预算，也不作废在途的请求
        try:
            if lane is self._hero:
                return build_hero_message(pending.state, self._cards, pending.rerolled)
            if lane is self._choice:
                return build_choice_message(pending.state, self._cards, self._memory)
            assert pending.ctx is not None
            return build_user_message(
                pending.state,
                pending.ctx,
                self._cards,
                self._memory,
                opponents_block=self._opponents_block(pending.state),
            )
        except Exception:
            log.warning("生成 AI 局面消息时出错", exc_info=True)
            notice_lane = "choice" if lane is self._choice else None
            self._events.put(AINotice("生成 AI 局面时出错，本次跳过", notice_lane))
            return None

    def _opponents_block(self, state: GameState) -> str:
        cached = self._opponents
        if cached is None or cached[0] != state.turn:
            block = build_opponents_block(state, self._cards, self._cfg.all_opponents)
            cached = (state.turn, block)
            self._opponents = cached
        return cached[1]

    def _start(self, lane: _LaneState, pending: _Pending, user: str) -> bool:
        """发出请求；预算用完、没有发出时返回 False。"""
        with self._lock:
            if self.calls >= self._cfg.max_calls_per_game:
                if not self._budget_notified:
                    self._budget_notified = True
                    self._events.put(AINotice(BUDGET_EXHAUSTED))
                return False
            self.calls += 1
            self._last_id += 1
            select, choice = pending.state.hero_select, pending.state.choice
            signature = select.signature() if lane is self._hero and select is not None else ()
            choice_id = choice.id if lane is self._choice and choice is not None else None
            request = AIRequest(
                self._last_id,
                pending.state,
                pending.ctx,
                self._clock(),
                lane.name,
                signature,
                choice_id,
            )
            lane.current_id = request.request_id
        lane.in_flight = request
        self._events.put(AIStarted(request.request_id, lane.name))
        self._run_async(lambda: self._work(lane, request, user))
        return True

    def _record(
        self,
        lane: _LaneState,
        request: AIRequest,
        call: _Call,
        result: LLMResult | None,
        outcome: str,
    ) -> None:
        if self._recorder is None:
            return
        self._recorder.write(
            {
                "request_id": request.request_id,
                "lane": lane.name,
                "turn": request.state.turn,
                "state_seq": request.state.seq,
                "model": self._cfg.model,
                "thinking": call.thinking,
                "system_sha1": self._system_of(lane)[1],
                # 改正调用只记改正要求：模型的回答在上一条记录的 response 里，局面消息在更早的那条
                "user": call.sent,
                "response": result.text if result else None,
                "elapsed": round(result.elapsed, 3) if result else None,
                "input_tokens": result.input_tokens if result else 0,
                "cached_tokens": result.cached_tokens if result else 0,
                "output_tokens": result.output_tokens if result else 0,
                "outcome": outcome,
                "repair": call.repair,
            }
        )

    def _system_of(self, lane: _LaneState) -> tuple[str, str]:
        """(系统提示, 它的哈希)：选英雄、选项各有自己的，招募的两路共用一个。"""
        if lane is self._hero:
            return self._hero_system, self._hero_hash
        if lane is self._choice:
            return self._choice_system, self._choice_hash
        return self._system, self._system_hash

    def _finish(
        self,
        request: AIRequest,
        plan=None,
        error=None,
        llm=None,
        expired=None,
        hero=None,
        choice=None,
        repaired: bool = False,
    ) -> None:
        elapsed = self._clock() - request.started
        self._events.put(
            AIResult(request, plan, error, elapsed, llm, expired, hero, choice, repaired)
        )

    def _parse(
        self, lane: _LaneState, request: AIRequest, text: str
    ) -> Plan | HeroAdvice | ChoiceAdvice:
        if lane is self._choice:
            choice = request.state.choice
            offered = [o.entity_id for o in choice.options] if choice is not None else []
            return parse_choice_pick(text, offered, request.choice_id or 0)
        if lane is not self._hero:
            return parse_plan(text)
        select = request.state.hero_select
        offered = [h.entity_id for h in select.offered] if select is not None else []
        return parse_hero_pick(text, offered, request.signature)

    def _problem(self, request: AIRequest, parsed: Plan | HeroAdvice | ChoiceAdvice) -> str | None:
        """招募两路的方案跑校验器；选英雄、选项没有动作序列（解析时已经核对过编号）。"""
        if not isinstance(parsed, Plan):
            return None
        assert request.ctx is not None
        return validate_plan(parsed.actions, request.ctx)

    def _deliver(
        self,
        request: AIRequest,
        parsed: Plan | HeroAdvice | ChoiceAdvice,
        result: LLMResult,
        repaired: bool,
    ) -> None:
        if isinstance(parsed, HeroAdvice):
            self._finish(request, llm=result, hero=parsed, repaired=repaired)
        elif isinstance(parsed, ChoiceAdvice):
            self._finish(request, llm=result, choice=parsed, repaired=repaired)
        else:
            assert request.ctx is not None
            actions = normalize_actions(parsed.actions, request.ctx)
            plan = parsed.model_copy(update={"actions": actions})
            self._finish(request, plan=plan, llm=result, repaired=repaired)

    def _thoughts(self, lane: _LaneState, plan: Plan | None) -> Plan | None:
        """深度分析的操作不能用时，它的分析和要找的牌仍然有用：去掉动作的方案；没有时为 None。"""
        if lane is not self._deep or plan is None or not (plan.analysis or plan.look_for):
            return None
        return plan.model_copy(update={"actions": ()})

    def _take_call(
        self, lane: _LaneState, request: AIRequest, deadline: float, margin: float
    ) -> bool:
        """再调用一次（空内容重试、改正）的共同条件：请求还是当前的（刷新、换组、新回合之后不再
        为旧局面花钱）、离截止时间还有 margin 秒、预算还有。满足时占一次调用并返回 True。"""
        with self._lock:
            ok = (
                lane.is_current(request)
                and self._clock() <= deadline - margin
                and self.calls < self._cfg.max_calls_per_game
            )
            if ok:
                self.calls += 1
        return ok

    def _work(self, lane: _LaneState, request: AIRequest, user: str) -> None:
        deadline = request.started + lane.timeout
        system = self._system_of(lane)[0]
        call = _Call(user, lane.thinking, lane.max_tokens)
        turns: tuple[Turn, ...] = ()
        retried = False
        first_plan: Plan | None = None  # 深度：第一次解析出的方案（改正失败时用它的思路）
        try:
            while True:
                timeout = max(deadline - self._clock(), 1.0)
                result = self._provider.complete_json(
                    system,
                    user,
                    timeout,
                    thinking=call.thinking,
                    max_tokens=call.max_tokens,
                    turns=turns,
                )
                with self._lock:
                    self.input_tokens += result.input_tokens
                    self.output_tokens += result.output_tokens
                    self._auth_failures = 0
                parsed: Plan | HeroAdvice | ChoiceAdvice | None = None
                try:
                    parsed = self._parse(lane, request, result.text)
                except EmptyResponse:
                    # 空内容没有可改的东西：同样的消息（改正时是同样的改正对话）再发一次
                    self._record(lane, request, call, result, "空内容")
                    if (
                        lane.retry_empty
                        and not retried
                        and self._take_call(lane, request, deadline, 1.0)
                    ):
                        retried = True
                        continue
                    self._give_up(lane, request, result, EMPTY, call.repair, first_plan)
                    return
                except ParseError as exc:
                    problem = str(exc)
                else:
                    problem = self._problem(request, parsed)
                    if first_plan is None and isinstance(parsed, Plan):
                        first_plan = parsed
                if problem is None:
                    assert parsed is not None
                    self._record(lane, request, call, result, "合法")
                    self._deliver(request, parsed, result, call.repair)
                    return
                self._record(lane, request, call, result, f"无效：{problem}")
                if (
                    self._cfg.repair_invalid
                    and not call.repair
                    and self._take_call(lane, request, deadline, REPAIR_MIN_SECONDS)
                ):
                    # 校验器仍然是唯一的标准：不从不合法的方案里挑合法的前几步
                    sent = repair_text(lane.name, problem)
                    turns = (("assistant", result.text), ("user", sent))
                    call = _Call(sent, lane.thinking, lane.max_tokens, repair=True)
                    if lane is self._deep:  # 思考在第一次回答里做完了，改正只修动作
                        tokens = max(self._cfg.max_output_tokens, DEEP_REPAIR_MIN_TOKENS)
                        call = replace(call, thinking="disabled", max_tokens=tokens)
                    self._events.put(AIRepairing(request.request_id, lane.name, problem))
                    continue
                error = INVALID_AFTER_REPAIR if call.repair else INVALID
                self._give_up(lane, request, result, error, call.repair, first_plan)
                return
        except LLMError as exc:
            self._record(lane, request, call, None, f"失败：{exc}")
            if exc.kind == "auth":
                with self._lock:
                    self._auth_failures += 1
                    trip = (
                        self._auth_failures >= AUTH_FAILURE_LIMIT and self.disabled_reason is None
                    )
                    if trip:
                        self.disabled_reason = AUTH_DISABLED
                if trip:
                    self._events.put(AINotice(AUTH_DISABLED))
            thoughts = self._thoughts(lane, first_plan) if call.repair else None
            if thoughts is not None:  # 改正调用失败：深度分析第一次回答的思路照样交付
                self._finish(request, plan=thoughts, expired="invalid", repaired=True)
            else:
                self._finish(request, error=str(exc), repaired=call.repair)
        except Exception:
            log.warning("AI 调用出错", exc_info=True)
            self._record(lane, request, call, None, "失败：程序出错")
            self._finish(request, error="AI 调用出错", repaired=call.repair)

    def _give_up(
        self,
        lane: _LaneState,
        request: AIRequest,
        result: LLMResult,
        error: str,
        repaired: bool,
        first_plan: Plan | None,
    ) -> None:
        """没有可用的回答（不合法或空内容，不再改正、重试）。深度分析只看思路：思路取第一次
        解析出的方案（有思考的那次），没有分析和要找时才按错误结束。"""
        thoughts = self._thoughts(lane, first_plan)
        if thoughts is not None:
            self._finish(request, plan=thoughts, llm=result, expired="invalid", repaired=repaired)
        else:
            self._finish(request, error=error, llm=result, repaired=repaired)
