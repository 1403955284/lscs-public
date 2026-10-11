"""决定何时调用大模型，并在后台执行（M2 设计文档第 7.4 节）。

- observe() 只记下待发的请求，由主循环每次轮询调用的 tick() 发出。watch 启动时会从头读
  Power.log，一次轮询里重放很多回合；这样只有读完后的最终局面才会真的调用；
- 招募开始：下一次 tick() 立即调用；
- 刷新（商店实体编号与上次完全不重合）：商店稳定 debounce 秒后调用，期间的买卖会更新待发的局面；
  刷新前发出、还没返回的请求作废（方案里的牌已经不在商店了）；
- 同一时间只认最新的请求，旧请求的结果直接丢弃；
- 请求超过 timeout_seconds + 2 秒还没返回：提示"AI 超时"并作废；
- 每局最多 max_calls_per_game 次（含空内容重试）；
- Key 连续 3 次认证失败后关闭 AI。
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
from lscs.advisor.models import Plan
from lscs.advisor.validate import validate_plan
from lscs.cards.db import CardDB
from lscs.config import LLMConfig
from lscs.llm.notices import AUTH_DISABLED, AUTH_FAILURE_LIMIT, BUDGET_EXHAUSTED
from lscs.llm.parse import EmptyResponse, ParseError, parse_plan
from lscs.llm.prompt import build_system_prompt, build_user_message
from lscs.llm.provider import LLMError, LLMProvider, LLMResult
from lscs.llm.record import Recorder
from lscs.state.models import Event, EventType, GameState, Phase

log = logging.getLogger(__name__)

# openai 的 timeout 是单次读取的超时，DeepSeek 的保活字节会让整次调用拖得更久；超过这个余量就放弃
DEADLINE_GRACE = 2.0


@dataclass(frozen=True)
class AIRequest:
    request_id: int
    state: GameState
    ctx: TurnContext
    rule_plans: tuple[Plan, ...]
    started: float


@dataclass(frozen=True)
class _Pending:
    state: GameState
    ctx: TurnContext
    plans: tuple[Plan, ...]
    due: float  # 到这个时间（clock）之后的第一次 tick() 发出


@dataclass(frozen=True)
class AIStarted:
    request_id: int


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


SchedulerEvent = AIStarted | AINotice | AIResult


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
        self._current_id = 0
        self._auth_failures = 0
        self.disabled_reason: str | None = None
        self.reset_game()

    def reset_game(self) -> None:
        # 上一局还没返回的请求全部作废
        self._stale_up_to = self._current_id
        with self._lock:
            self.calls = 0
            self.input_tokens = 0
            self.output_tokens = 0
        self._budget_notified = False
        self._pending: _Pending | None = None
        self._in_flight: AIRequest | None = None
        self._last_shop: frozenset[int] = frozenset()
        self._last_turn: int | None = None

    def _is_current(self, request: AIRequest) -> bool:
        return self._stale_up_to < request.request_id == self._current_id

    def observe(
        self,
        state: GameState,
        events: Sequence[Event],
        ctx: TurnContext | None,
        plans: Sequence[Plan],
    ) -> None:
        """每次状态更新都调用；只记下要发的请求，不在这里发出。"""
        if state.phase != Phase.RECRUIT:
            self._pending = None
            return
        shop = frozenset(s.entity_id for s in state.shop)
        refreshed = (
            bool(shop)
            and bool(self._last_shop)
            and not (shop & self._last_shop)
            and state.turn == self._last_turn
        )
        started = any(e.type == EventType.RECRUIT_STARTED for e in events)
        if refreshed and not started:
            self._stale_up_to = self._current_id  # 刷新前的请求作废（第 7.5 节）
        if ctx is None:
            pass  # 没有 ctx 的局面不能生成请求；保留之前待发的请求
        elif started:
            self._pending = _Pending(state, ctx, tuple(plans), self._clock())
        elif refreshed:
            self._pending = _Pending(state, ctx, tuple(plans), self._clock() + self._debounce)
        elif self._pending is not None:
            # 还没发出请求时又买卖了：用最新的局面，时间不变
            self._pending = replace(self._pending, state=state, ctx=ctx, plans=tuple(plans))
        if shop or state.turn != self._last_turn:
            self._last_shop = shop  # 买空商店时保留上次的商品，之后刷新仍能识别
        self._last_turn = state.turn

    def tick(self) -> list[SchedulerEvent]:
        """主循环每次轮询调用：发出到期的请求，取回已完成的结果，处理超时。"""
        pending = self._pending
        if pending is not None and self._clock() >= pending.due:
            self._pending = None
            self._start(pending.state, pending.ctx, pending.plans)
        out: list[SchedulerEvent] = []
        while True:
            try:
                event = self._events.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, AIResult):
                if not self._is_current(event.request):
                    log.info("丢弃已被取代的 AI 结果 #%s", event.request.request_id)
                    continue
                self._in_flight = None
            out.append(event)
        request = self._in_flight
        if request is not None:
            waited = self._clock() - request.started
            if not self._is_current(request):
                self._in_flight = None
            elif waited > self._cfg.timeout_seconds + DEADLINE_GRACE:
                log.info("AI 请求 #%s 等了 %.1f 秒还没返回，放弃", request.request_id, waited)
                self._stale_up_to = request.request_id
                self._in_flight = None
                out.append(AIResult(request, None, "AI 超时", waited))
        return out

    def _start(self, state: GameState, ctx: TurnContext, plans: tuple[Plan, ...]) -> None:
        if self.disabled_reason is not None:
            return
        # 先生成局面消息：出错时不占预算，也不作废在途的请求
        try:
            user = build_user_message(state, ctx, self._cards, plans)
        except Exception:
            log.warning("生成 AI 局面消息时出错", exc_info=True)
            self._events.put(AINotice("生成 AI 局面时出错，本次跳过"))
            return
        with self._lock:
            if self.calls >= self._cfg.max_calls_per_game:
                if not self._budget_notified:
                    self._budget_notified = True
                    self._events.put(AINotice(BUDGET_EXHAUSTED))
                return
            self.calls += 1
        self._current_id += 1
        request = AIRequest(self._current_id, state, ctx, plans, self._clock())
        self._in_flight = request
        self._events.put(AIStarted(request.request_id))
        self._run_async(lambda: self._work(request, user))

    def _record(
        self, request: AIRequest, user: str, result: LLMResult | None, outcome: str
    ) -> None:
        if self._recorder is None:
            return
        self._recorder.write(
            {
                "request_id": request.request_id,
                "turn": request.state.turn,
                "state_seq": request.state.seq,
                "model": self._cfg.model,
                "thinking": self._cfg.thinking,
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

    def _finish(self, request: AIRequest, plan=None, error=None, llm=None) -> None:
        elapsed = self._clock() - request.started
        self._events.put(AIResult(request, plan, error, elapsed, llm))

    def _work(self, request: AIRequest, user: str) -> None:
        deadline = request.started + self._cfg.timeout_seconds
        retried = False
        try:
            while True:
                timeout = max(deadline - self._clock(), 1.0)
                result = self._provider.complete_json(self._system, user, timeout)
                with self._lock:
                    self.input_tokens += result.input_tokens
                    self.output_tokens += result.output_tokens
                    self._auth_failures = 0
                try:
                    plan = parse_plan(result.text)
                except EmptyResponse:
                    self._record(request, user, result, "空内容")
                    with self._lock:
                        can_retry = (
                            not retried
                            and self._is_current(request)
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
                    self._record(request, user, result, f"无效：{exc}")
                    self._finish(request, error="AI 建议无效，已忽略", llm=result)
                    return
                problem = validate_plan(plan.actions, request.ctx)
                if problem is not None:
                    self._record(request, user, result, f"无效：{problem}")
                    self._finish(request, error="AI 建议无效，已忽略", llm=result)
                    return
                self._record(request, user, result, "合法")
                self._finish(request, plan=plan, llm=result)
                return
        except LLMError as exc:
            self._record(request, user, None, f"失败：{exc}")
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
            self._record(request, user, None, "失败：程序出错")
            self._finish(request, error="AI 调用出错")
