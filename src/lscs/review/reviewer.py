"""把复盘接到 Pipeline 的状态更新上（对局复盘设计文档第 8 节）。

- on_update：喂 RecordBuilder；新对局清掉上一局还没生成的待办；对局结束时记下待办。
- tick：主循环每次轮询调用；有待办才生成复盘（提取 → 提醒 → 关键时刻 → 历史 → 文件 →
  display.show_review），只出一次。watch 启动时从头读日志，一次轮询里可能过完好几局，
  只有读完时已经结束的那一局会出复盘；更早的对局不出。
- tap：挂进 DisplayGroup 的 AdviceSink，只记下本局显示过的建议，不显示。
- AI 复盘（有提供商、[review] ai 打开时）：规则版交付之后在工作线程里拼消息、调用、解析校验
  （最多改正一次）、写调用记录，结果放进队列；tick 取出后在监听线程里重写文件、再交给显示端。
  超过 deep_timeout_seconds + 2 秒还没回来就算超时，之后回来的结果丢弃。

显示端的方法都在监听线程（调用 on_update、tick 的线程）里调用。任何一步出错只记日志、
终端一行"复盘出错：……"，不能让 watch 退出。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from lscs.advisor.core_cards import CoreMarks
from lscs.advisor.display import AdviceSink
from lscs.advisor.models import (
    Advice,
    ChoiceAdvice,
    GamePlan,
    HeroAdvice,
    Lane,
    PositionAdvice,
    StatsNote,
)
from lscs.advisor.opponents import OpponentOverview
from lscs.advisor.rules_config import RulesConfig
from lscs.cards.db import CardDB
from lscs.config import LLMConfig, ReviewConfig
from lscs.llm.notices import TIMEOUT
from lscs.llm.provider import LLMProvider
from lscs.llm.record import Recorder
from lscs.llm.scheduler import DEADLINE_GRACE
from lscs.review.ai import (
    ReviewCall,
    ReviewResult,
    build_review_message,
    build_review_system_prompt,
    run_review,
)
from lscs.review.record import AdviceLog, GameRecord, Op, RecordBuilder, attach_advice
from lscs.review.report import (
    HISTORY_FILE,
    GameReview,
    already_reviewed,
    append_history,
    history_entry,
    read_history,
    review_game,
    review_markdown,
    save_review,
    write_text,
)
from lscs.state.models import Event, EventType, GameState

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
Output = Callable[[str], None]


def _print(line: str) -> None:
    print(line, flush=True)


def _run_in_thread(job: Callable[[], None]) -> None:
    threading.Thread(target=job, name="lscs-review", daemon=True).start()


class AdviceCollector:
    """AdviceSink：只记不显示。回合开始的第一条规则建议、没过期的快速建议、最后一条深度分析、
    选英雄和选项的 AI 推荐（卡牌 ID），以及整局计划的每次定下和改变（到达时的回合）。"""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.logs: dict[int, AdviceLog] = {}
        self.hero_ai_pick: str | None = None
        self.ai_choices: dict[int, str] = {}
        self.plans: list[tuple[int, GamePlan]] = []

    def _log(self, turn: int) -> AdviceLog:
        return self.logs.setdefault(turn, AdviceLog())

    def show_opponents(self, state: GameState, overview: OpponentOverview) -> None:
        pass

    def show_rules(self, state: GameState, advice: Advice) -> None:
        log_ = self._log(advice.turn)
        if log_.rules_start is None:
            log_.rules_start = (advice.state_seq, advice.plan)

    def rules_error(self, hero: bool = False) -> None:
        pass

    def ai_pending(self, lane: Lane = "fast") -> None:
        pass

    def ai_text(self, text: str, lane: Lane = "fast") -> None:
        pass

    def ai_repairing(self, lane: Lane, reason: str) -> None:
        pass

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        if advice.lane == "deep":
            self._log(advice.turn).deep = advice  # 后到的覆盖
        elif advice.lane == "fast" and advice.expired is None:
            self._log(advice.turn).fast.append((advice.state_seq, advice.plan))

    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None:
        if advice.source != "llm" or advice.pick is None or state.hero_select is None:
            return
        hero = next((h for h in state.hero_select.offered if h.entity_id == advice.pick), None)
        if hero is not None:
            self.hero_ai_pick = hero.card_id

    def show_choice(self, state: GameState, advice: ChoiceAdvice | None) -> None:
        if advice is None or state.choice is None:
            return
        option = next((o for o in state.choice.options if o.entity_id == advice.pick), None)
        if option is not None:
            self.ai_choices[advice.choice_id] = option.card_id

    def show_plan(self, state: GameState, plan: GamePlan, previous: GamePlan | None) -> None:
        self.plans.append((state.turn, plan))

    def ai_summary(
        self, calls: int, input_tokens: int, output_tokens: int, off_plan: int = 0
    ) -> None:
        pass

    def show_review(self, state: GameState, review: GameReview) -> None:
        pass

    def show_position(self, state: GameState, advice: PositionAdvice | None) -> None:
        pass  # 复盘不评站位（站位建议设计文档第 10 节）

    def show_stats(self, state: GameState, note: StatsNote) -> None:
        pass  # 复盘不用本版本统计（本版本统计设计文档第 13 节）

    def show_core_cards(self, state: GameState, marks: CoreMarks | None) -> None:
        pass  # 复盘不用核心卡标记（流派指南自动更新设计文档第 9 节）


@dataclass(frozen=True)
class _Pending:
    """对局结束时记下的待办：记录、操作和本局显示过的建议。"""

    record: GameRecord
    ops: tuple[Op, ...]
    entity_cards: dict[int, str]
    logs: dict[int, AdviceLog]
    hero_ai_pick: str | None
    ai_choices: dict[int, str] = field(default_factory=dict)
    when: datetime = field(default_factory=datetime.now)
    plans: tuple[tuple[int, GamePlan], ...] = ()


class GameReviewer:
    def __init__(
        self,
        cfg: ReviewConfig,
        rules: RulesConfig,
        name: Namer,
        display: AdviceSink,
        cards: CardDB | None = None,
        provider: LLMProvider | None = None,
        llm: LLMConfig | None = None,
        guide: str = "",
        recorder: Recorder | None = None,
        *,
        directory: Path,
        base: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        run_async: Callable[[Callable[[], None]], None] = _run_in_thread,
        save_history: bool = True,
        out: Output = _print,
        ai_blocked: Callable[[], str | None] = lambda: None,
    ) -> None:
        """cards：卡牌数据（金色卡对应的普通卡、校验 AI 写的卡名）；provider、llm：AI 复盘用的
        提供商和配置，provider 为 None 或 [review] ai 关掉时只出规则版；guide：攻略要点；
        recorder：调用记录。directory：复盘文件和 history.jsonl 的目录；base：显示路径时相对它
        （项目目录）；save_history：把这局追加进 history.jsonl（lscs review 默认不追加）；
        out：出错时打印一行；ai_blocked：调度器关掉 AI 的原因（Key 连续无效），有原因时不调 AI。"""
        self._cfg = cfg
        self._rules = rules
        self._name = name
        self._display = display
        self._cards = cards
        self._provider = provider
        self._llm = llm
        self._recorder = recorder
        self._directory = directory
        self._base = base
        self._clock = clock
        self._now = now
        self._run_async = run_async
        self._save_history = save_history
        self._out = out
        self._ai_blocked = ai_blocked
        self._ai_on = cfg.ai and provider is not None and llm is not None and cards is not None
        self._system = build_review_system_prompt(guide) if self._ai_on else ""
        self._builder = RecordBuilder(
            rules.buy_cost_for, cards.normal_id if cards is not None else None
        )
        self._collector = AdviceCollector()
        self._pending: _Pending | None = None
        self._state: GameState | None = None
        self._results: queue.Queue[tuple[int, ReviewResult]] = queue.Queue()
        self._job = 0
        self._waiting: tuple[int, GameReview, float] | None = None  # (编号, 规则版, 开始时间)
        self.last_review: GameReview | None = None
        self.already_done = False  # 这次读到的对局之前已经复盘过（watch 重启、重复 --save-history）

    @property
    def ai_enabled(self) -> bool:
        return self._ai_on

    @property
    def tap(self) -> AdviceSink:
        return self._collector

    @property
    def history_path(self) -> Path:
        return self._directory / HISTORY_FILE

    def on_update(self, state: GameState, events: list[Event]) -> None:
        self._state = state
        types = {e.type for e in events}
        if EventType.GAME_STARTED in types:
            self._pending = None  # 上一局的复盘还没出就开了新的一局：不出了
            self._collector.reset()
        try:
            self._builder.on_update(state, events)
            if EventType.GAME_ENDED in types:
                self._game_ended()
        except Exception:
            log.warning("提取对局记录时出错", exc_info=True)

    def _game_ended(self) -> None:
        record = self._builder.finish()
        if record is None:
            return
        collector = self._collector
        self._pending = _Pending(
            record=record,
            ops=self._builder.ops,
            entity_cards=dict(self._builder.entity_cards),
            logs=dict(collector.logs),
            hero_ai_pick=collector.hero_ai_pick,
            ai_choices=dict(collector.ai_choices),
            when=self._now(),
            plans=tuple(collector.plans),
        )

    def tick(self) -> None:
        """主循环每次轮询调用：有待办时出规则版复盘（并发出 AI 复盘），取回 AI 复盘的结果。"""
        pending, self._pending = self._pending, None
        try:
            if pending is not None:
                self._deliver(pending)
            self._drain()
            self._check_timeout()
        except Exception as exc:
            log.warning("生成复盘时出错", exc_info=True)
            self._out(f"  📝 复盘出错：{exc or type(exc).__name__}")

    def _show(self, review: GameReview) -> None:
        self.last_review = review
        self._display.show_review(self._state if self._state is not None else GameState(), review)

    def _deliver(self, pending: _Pending) -> None:
        record = attach_advice(
            pending.record,
            pending.ops,
            pending.entity_cards,
            pending.logs,
            hero_ai_pick=pending.hero_ai_pick,
            ai_choices=pending.ai_choices,
            plans=pending.plans,
        )
        history = read_history(self.history_path)
        review = review_game(
            record, self._rules, self._name, pending.when, history, self._cfg.history_games
        )
        if self._save_history:
            # watch 重新启动时从头读同一份 Power.log：最后那局已经复盘过了，
            # 不再写文件、记历史、调 AI
            old = already_reviewed(history_entry(review), history)
            if old is not None:
                self._out(f"  📝 这局已经复盘过：{old.get('file') or '（当时没能保存文件）'}")
                self.already_done = True
                return
        review = save_review(review, self._directory, self._name, self._base)
        if self._save_history:
            try:
                append_history(self.history_path, history_entry(review))
            except OSError:
                log.warning("写入复盘历史失败", exc_info=True)
        use_ai = self._ai_on and self._ai_blocked() is None
        if use_ai:
            review = replace(review, ai_state="pending")
        self._show(review)
        if use_ai:
            self._start_ai(review)

    # ---- AI 复盘 ----

    def _start_ai(self, review: GameReview) -> None:
        assert self._provider is not None and self._llm is not None
        self._job += 1
        job = self._job
        self._waiting = (job, review, self._clock())
        call = ReviewCall(
            provider=self._provider,
            model=self._llm.model,
            thinking=self._llm.deep_thinking,
            timeout=self._llm.deep_timeout_seconds,
            recorder=self._recorder,
            repair=self._llm.repair_invalid,
        )
        name, cards, system, clock, results = (
            self._name,
            self._cards,
            self._system,
            self._clock,
            self._results,
        )

        def work() -> None:
            # 工作线程：只读 review（不可变）、卡牌数据和提供商，结果放进队列
            try:
                user = build_review_message(
                    review.record, review.flags, review.moments, name, review.habits
                )
                result = run_review(call, system, user, review.record, cards, clock)
            except Exception:
                log.warning("AI 复盘出错", exc_info=True)
                result = ReviewResult(None, "AI 复盘出错", 0.0)
            results.put((job, result))

        self._run_async(work)

    def _drain(self) -> None:
        while True:
            try:
                job, result = self._results.get_nowait()
            except queue.Empty:
                return
            waiting = self._waiting
            if waiting is None or waiting[0] != job:
                log.info("AI 复盘 #%s 回来时已经超时，丢弃", job)
                continue
            self._waiting = None
            self._finish_ai(waiting[1], result)

    def _check_timeout(self) -> None:
        waiting = self._waiting
        if waiting is None or self._llm is None:
            return
        waited = self._clock() - waiting[2]
        if waited > self._llm.deep_timeout_seconds + DEADLINE_GRACE:
            self._waiting = None
            self._finish_ai(waiting[1], ReviewResult(None, TIMEOUT, waited))

    def _finish_ai(self, review: GameReview, result: ReviewResult) -> None:
        if result.review is None:
            self._show(replace(review, ai_state="failed", ai_error=result.error))
            return
        done = replace(review, ai=result.review, ai_state="done", ai_elapsed=result.elapsed)
        if review.path is not None:
            try:
                write_text(review.path, review_markdown(done, self._name))
            except OSError as exc:
                log.warning("重写复盘文件失败", exc_info=True)
                done = replace(done, save_error=exc.strerror or str(exc))
        self._show(done)
