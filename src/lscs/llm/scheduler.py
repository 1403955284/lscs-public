"""决定何时调用大模型，并在后台执行（M2 设计文档第 7.4 节，更聪明的 AI 建议设计文档第 3.3 节）。

两路调用，各自有当前请求、在途请求和超时；每局预算和认证失败计数两路共用：
- 快速（fast）：按 thinking / max_output_tokens / timeout_seconds 调用，2~3 秒出结果。
  招募开始时立即调用；刷新（商店实体编号与上次完全不重合）后，商店稳定 debounce 秒再调用，
  期间的买卖会更新待发的局面；刷新前发出、还没返回的快速请求作废（方案里的牌已经不在商店了）；
  返回空内容时重试一次。
- 深度（deep，deep_analysis 打开时）：按 deep_thinking / deep_max_output_tokens /
  deep_timeout_seconds 调用，约 20 秒。只在招募开始时和快速请求同时发出；不重试；
  刷新后不作废——操作过期，但分析仍然有用，结果照常交付并标明 expired="refreshed"。

其他规则：
- observe() 只记下待发的请求，由主循环每次轮询调用的 tick() 发出。watch 启动时会从头读
  Power.log，一次轮询里重放很多回合；这样只有读完后的最终局面才会真的调用（每路最多一次）；
- 每路同一时间只认最新的请求，旧请求的结果不显示；新回合、新对局时两路的旧请求都作废
  （例外：上一回合晚到的深度分析不显示，但它的方向和要找的牌会记下来）；
- 请求超过该路的超时 + 2 秒还没返回：提示"AI 超时"并作废；
- 每局最多 max_calls_per_game 次（两路合计，含空内容重试）；
- Key 连续 3 次认证失败后关闭 AI；
- 记住本局最近一次合法结果的方向、要找的牌和分析（同一回合深度优先），附在之后的局面消息末尾。
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
from lscs.advisor.models import Expired, Lane, Plan
from lscs.advisor.validate import normalize_actions, validate_plan
from lscs.cards.db import CardDB
from lscs.config import LLMConfig
from lscs.llm.notices import AUTH_DISABLED, AUTH_FAILURE_LIMIT, BUDGET_EXHAUSTED, TIMEOUT
from lscs.llm.parse import EmptyResponse, ParseError, parse_plan
from lscs.llm.prompt import AIMemory, build_system_prompt, build_user_message
from lscs.llm.provider import LLMError, LLMProvider, LLMResult, Thinking
from lscs.llm.record import Recorder
from lscs.state.models import Event, EventType, GameState, Phase

log = logging.getLogger(__name__)

# openai 的 timeout 是单次读取的超时，DeepSeek 的保活字节会让整次调用拖得更久；超过这个余量就放弃
DEADLINE_GRACE = 2.0
INVALID = "AI 建议无效，已忽略"


@dataclass(frozen=True)
class AIRequest:
    request_id: int
    state: GameState
    ctx: TurnContext
    started: float
    lane: Lane = "fast"


@dataclass(frozen=True)
class _Pending:
    state: GameState
    ctx: TurnContext
    due: float  # 到这个时间（clock）之后的第一次 tick() 发出


@dataclass(frozen=True)
class AIStarted:
    request_id: int
    lane: Lane = "fast"


@dataclass(frozen=True)
class AINotice:
    text: str


@dataclass(frozen=True)
class AIResult:
    request: AIRequest
    plan: Plan | None
    error: str | None
    elapsed: float
    llm: LLMResult | None = None
    # 只有深度分析会有：plan 里的操作不能用，只看分析（refreshed 发出后商店刷新过；invalid 不合法）
    expired: Expired | None = None

    @property
    def lane(self) -> Lane:
        return self.request.lane


SchedulerEvent = AIStarted | AINotice | AIResult


@dataclass
class _LaneState:
    """一路调用的设置和状态。请求编号两路共用一个计数器（调用记录里不重复），
    但每路只认自己最新的请求。

    线程：所有状态字段只在主线程（observe / tick / reset_game）里写。后台线程只读设置
    （thinking 等，创建后不变），以及在重试前通过 is_current() 读 current_id、stale_up_to；
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
    in_flight: AIRequest | None = None

    def is_current(self, request: AIRequest) -> bool:
        return self.stale_up_to < request.request_id == self.current_id


def _run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="lscs-llm", daemon=True).start()


class LLMScheduler:
    def __init__(
        self,
        provider: LLMProvider,
        cfg: LLMConfig,
        cards: CardDB,
        guide: str,
        recorder: Recorder | None = None,
        debounce: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        run_async: Callable[[Callable[[], None]], None] = _run_in_thread,
    ) -> None:
        self._provider = provider
        self._cfg = cfg
        self._cards = cards
        self._system = build_system_prompt(guide)
        self._system_hash = hashlib.sha1(self._system.encode("utf-8")).hexdigest()[:12]
        self._recorder = recorder
        self._debounce = debounce
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
        self._lanes = (self._fast, self._deep)  # 同时到期时先发快速
        self._last_id = 0
        self._auth_failures = 0
        self.disabled_reason: str | None = None
        self.reset_game()

    @property
    def memory(self) -> AIMemory | None:
        return self._memory

    @property
    def deep_thinking(self) -> str:
        return self._cfg.deep_thinking

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

    def observe(self, state: GameState, events: Sequence[Event], ctx: TurnContext | None) -> None:
        """每次状态更新都调用；只记下要发的请求，不在这里发出。"""
        if state.phase != Phase.RECRUIT:
            for lane in self._lanes:
                lane.pending = None
            return
        shop = frozenset(s.entity_id for s in state.shop)
        refreshed = (
            bool(shop)
            and bool(self._last_shop)
            and not (shop & self._last_shop)
            and state.turn == self._last_turn
        )
        started = any(e.type == EventType.RECRUIT_STARTED for e in events)
        if started:
            with self._lock:
                for lane in self._lanes:
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
            for lane in self._lanes:
                if lane.pending is not None:
                    lane.pending = replace(lane.pending, state=state, ctx=ctx)
        if shop or state.turn != self._last_turn:
            self._last_shop = shop  # 买空商店时保留上次的商品，之后刷新仍能识别
        self._last_turn = state.turn

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
                lane.in_flight = None
            elif waited > lane.timeout + DEADLINE_GRACE:
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
            if isinstance(event, AIResult):
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
        return self._deep if request.lane == "deep" else self._fast

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
            return
        messages: dict[int, str | None] = {}  # 两路同时发出时局面相同，只生成一次
        for lane, pending in due:
            key = id(pending.state)
            if key not in messages:
                messages[key] = self._message(pending)
            user = messages[key]
            if user is not None:
                self._start(lane, pending, user)

    def _message(self, pending: _Pending) -> str | None:
        # 先生成局面消息：出错时不占预算，也不作废在途的请求
        try:
            return build_user_message(pending.state, pending.ctx, self._cards, self._memory)
        except Exception:
            log.warning("生成 AI 局面消息时出错", exc_info=True)
            self._events.put(AINotice("生成 AI 局面时出错，本次跳过"))
            return None

    def _start(self, lane: _LaneState, pending: _Pending, user: str) -> None:
        with self._lock:
            if self.calls >= self._cfg.max_calls_per_game:
                if not self._budget_notified:
                    self._budget_notified = True
                    self._events.put(AINotice(BUDGET_EXHAUSTED))
                return
            self.calls += 1
            self._last_id += 1
            request = AIRequest(self._last_id, pending.state, pending.ctx, self._clock(), lane.name)
            lane.current_id = request.request_id
        lane.in_flight = request
        self._events.put(AIStarted(request.request_id, lane.name))
        self._run_async(lambda: self._work(lane, request, user))

    def _record(
        self,
        lane: _LaneState,
        request: AIRequest,
        user: str,
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
                "thinking": lane.thinking,
                "system_sha1": self._system_hash,
                "user": user,
                "response": result.text if result else None,
                "elapsed": round(result.elapsed, 3) if result else None,
                "input_tokens": result.input_tokens if result else 0,
                "cached_tokens": result.cached_tokens if result else 0,
                "output_tokens": result.output_tokens if result else 0,
                "outcome": outcome,
            }
        )

    def _finish(self, request: AIRequest, plan=None, error=None, llm=None, expired=None) -> None:
        elapsed = self._clock() - request.started
        self._events.put(AIResult(request, plan, error, elapsed, llm, expired))

    def _work(self, lane: _LaneState, request: AIRequest, user: str) -> None:
        deadline = request.started + lane.timeout
        retried = False
        try:
            while True:
                timeout = max(deadline - self._clock(), 1.0)
                result = self._provider.complete_json(
                    self._system, user, timeout, thinking=lane.thinking, max_tokens=lane.max_tokens
                )
                with self._lock:
                    self.input_tokens += result.input_tokens
                    self.output_tokens += result.output_tokens
                    self._auth_failures = 0
                try:
                    plan = parse_plan(result.text)
                except EmptyResponse:
                    self._record(lane, request, user, result, "空内容")
                    with self._lock:
                        can_retry = (
                            lane.retry_empty
                            and not retried
                            and lane.is_current(request)
                            and self._clock() < deadline - 1
                            and self.calls < self._cfg.max_calls_per_game
                        )
                        if can_retry:
                            self.calls += 1
                    if can_retry:
                        retried = True
                        continue
                    self._finish(request, error="AI 返回了空内容", llm=result)
                    return
                except ParseError as exc:
                    self._record(lane, request, user, result, f"无效：{exc}")
                    self._finish(request, error=INVALID, llm=result)
                    return
                problem = validate_plan(plan.actions, request.ctx)
                if problem is not None:
                    self._record(lane, request, user, result, f"无效：{problem}")
                    if lane.name == "deep" and (plan.analysis or plan.look_for):
                        # 深度分析的操作不合法，但分析和要找的牌仍然有用
                        thoughts = plan.model_copy(update={"actions": ()})
                        self._finish(request, plan=thoughts, llm=result, expired="invalid")
                    else:
                        self._finish(request, error=INVALID, llm=result)
                    return
                self._record(lane, request, user, result, "合法")
                plan = plan.model_copy(
                    update={"actions": normalize_actions(plan.actions, request.ctx)}
                )
                self._finish(request, plan=plan, llm=result)
                return
        except LLMError as exc:
            self._record(lane, request, user, None, f"失败：{exc}")
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
            self._finish(request, error=str(exc))
        except Exception:
            log.warning("AI 调用出错", exc_info=True)
            self._record(lane, request, user, None, "失败：程序出错")
            self._finish(request, error="AI 调用出错")
