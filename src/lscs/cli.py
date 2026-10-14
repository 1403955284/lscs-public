"""命令行入口。"""

from __future__ import annotations

import argparse
import functools
import logging
import re
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import date, datetime
from datetime import time as day_time
from pathlib import Path

from lscs import __version__
from lscs.advisor.display import AdviceDisplay, AdviceSink, DisplayGroup, deep_wait_hint
from lscs.advisor.rules_config import RulesConfig, load_rules_config
from lscs.advisor.runner import AdviceRunner
from lscs.config import (
    DEFAULT_CONFIG,
    AppConfig,
    ConfigError,
    HighlightConfig,
    ReviewConfig,
    load_config,
    mask_key,
    old_highlight_values,
)
from lscs.dump import Namer, describe
from lscs.logwatch.logconfig import (
    default_log_config_path,
    fix_client_config,
    fix_log_config,
)
from lscs.logwatch.paths import find_install_dir, find_power_logs, latest_power_log
from lscs.logwatch.tail import LogTailer
from lscs.overlay import app as overlay_app
from lscs.overlay.model import OverlayModel
from lscs.pipeline import Pipeline, UpdateHandler
from lscs.replay import line_seconds, push_paced, read_log_lines
from lscs.review.reviewer import GameReviewer
from lscs.state.models import Event, GameState
from lscs.state.pool import load_pool_config
from lscs.state.tracker import StateTracker
from lscs.stats.model import MetaStats
from lscs.stats.store import StatsStore, start_text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POOL = PROJECT_ROOT / "strategy" / "current" / "pool.toml"
DEFAULT_RULES = PROJECT_ROOT / "strategy" / "current" / "rules.toml"
DEFAULT_GUIDE = PROJECT_ROOT / "strategy" / "current" / "guide.md"
DEFAULT_HEROES = PROJECT_ROOT / "strategy" / "current" / "heroes.md"  # 可选：当月英雄强度资料
RECORDINGS_DIR = PROJECT_ROOT / "recordings"
REVIEW_BASE = PROJECT_ROOT  # [review] dir 是相对路径时相对它（测试里换成临时目录）
# 本版本统计的缓存目录；None 表示和卡牌数据同一个缓存目录（测试里换成临时目录）
STATS_CACHE_DIR: Path | None = None
POLL_INTERVAL = 0.2  # watch 默认的轮询间隔（秒）
OVERLAY_REPLAY_SPEED = 20.0  # replay --overlay 没填 --speed 时的倍速（不等待的话面板一闪而过）
RECOMMENDED_OUTPUT_TOKENS = 1500  # 快速建议要写分析和 JSON，800 容易截断
RECOMMENDED_CALLS_PER_GAME = 100  # 一局大约 60~70 次调用
# watch 启动时说明"⚔ 本场已定"是剧透不是预测；where：有面板时"棋盘顶部和这里"，否则"这里"
COMBAT_RESULT_NOTICE = (
    "战斗一开始，{where}会先显示这场的真实结果（日志比画面快，不是预测）；"
    "不想被剧透把 config.toml 的 [combat_result] enabled 改成 false。"
)
log = logging.getLogger(__name__)


def _fix_client_config(args: argparse.Namespace) -> None:
    """解除 10MB 日志上限；失败只警告，不影响 log.config 的结果。"""
    install = find_install_dir(Path(args.install_dir) if args.install_dir else None)
    if install is None:
        print(
            "找不到炉石安装目录，未能解除 Power.log 的 10MB 大小上限，"
            "请用 --install-dir 指定后重试。"
        )
        return
    path = install / "client.config"
    try:
        changed = fix_client_config(install)
    except PermissionError:
        print(
            f"没有权限写入 {path}。请以管理员身份运行 PowerShell 后重试，"
            "或手动创建该文件，内容为两行：\n[Log]\nFileSizeLimit.Int=-1"
        )
        return
    if changed:
        print(
            f"已写入 {path}（解除 Power.log 的 10MB 大小上限，"
            f"原文件如存在已备份为 {path.name}.bak）。"
        )
    else:
        print(f"{path} 已经正确配置，无需修改。")


def _cmd_fix_log_config(args: argparse.Namespace) -> int:
    path = Path(args.path) if args.path else default_log_config_path()
    if fix_log_config(path):
        print(f"已写入 {path}（原文件如存在已备份为 {path.name}.bak）。请重启炉石传说。")
    else:
        print(f"{path} 已经正确配置，无需修改。")
    _fix_client_config(args)
    return 0


def _install_dir(args: argparse.Namespace) -> Path | None:
    install = find_install_dir(Path(args.install_dir) if args.install_dir else None)
    if install is None:
        print("找不到炉石安装目录，请用 --install-dir 指定。")
    return install


def _cmd_find_logs(args: argparse.Namespace) -> int:
    install = _install_dir(args)
    if install is None:
        return 1
    logs = find_power_logs(install)
    if not logs:
        print(f"{install} 下没有 Power.log。请先运行 fix-log-config，重启炉石后打一局。")
        return 1
    for path in logs:
        print(path)
    return 0


def _identity(card_id: str) -> str:
    return card_id


def _load_cards():
    from lscs.cards.db import CardDB
    from lscs.cards.fetch import default_cache_dir, ensure_cards_json

    print("正在准备卡牌数据（首次约 10MB）…", flush=True)
    try:
        return CardDB.from_json_file(ensure_cards_json(default_cache_dir()))
    except Exception as exc:
        log.warning("加载卡牌数据失败", exc_info=True)
        print(f"无法加载卡牌数据（{exc}），将显示卡牌 ID。", flush=True)
        return None


def _stats_store(sync: bool) -> StatsStore | None:
    """config.toml 的 [stats]（本版本统计设计文档第 5.5 节）：打开时创建、打印一行、开始准备
    （sync：replay / review 同步准备，watch 在后台）；关闭（默认）时返回 None，不打印。
    config.toml 有误时打印原因，这次没有统计，其他照常。"""
    try:
        cfg = load_config(DEFAULT_CONFIG).stats
    except ConfigError as exc:
        print(f"{exc}（这次没有本版本统计）", flush=True)
        return None
    if not cfg.enabled:
        return None
    from lscs.cards.fetch import default_cache_dir

    store = StatsStore(cfg, STATS_CACHE_DIR if STATS_CACHE_DIR is not None else default_cache_dir())
    print(start_text(cfg, sync), flush=True)
    store.start(sync)
    return store


def _stats_getter(store: StatsStore | None) -> Callable[[], MetaStats | None] | None:
    """交给运行器和调度器的取统计的函数；统计关闭时为 None。"""
    return store.get if store is not None else None


def _namer(args: argparse.Namespace) -> Namer:
    if not args.names:
        return _identity
    cards = _load_cards()
    return cards.name if cards is not None else _identity


def _pool_estimator(cards, args: argparse.Namespace):
    """有卡牌数据时用查过卡牌库的卡池估计（对子、要找的牌还剩约几张）；没有时为 None，
    "还剩约 N 张"都不显示。"""
    if cards is None:
        return None
    from lscs.advisor.pool import PoolEstimator

    return PoolEstimator(cards, load_pool_config(Path(args.pool)).copies)


def _timeline(name: Namer, verbose: bool, combat_result: bool = True) -> UpdateHandler:
    """combat_result：打不打"⚔ 本场已定"那一行（watch 按 [combat_result]，replay 总是打）。"""

    def on_update(state: GameState, events: list[Event]) -> None:
        for line in describe(state, events, name, verbose=verbose, combat_result=combat_result):
            print(line, flush=True)

    return on_update


def _combine(*handlers: UpdateHandler) -> UpdateHandler:
    """依次调用每个处理函数；一个出错（比如时间线）不影响后面的（比如建议）。"""

    def on_update(state: GameState, events: list[Event]) -> None:
        for handler in handlers:
            try:
                handler(state, events)
            except Exception:
                log.warning("输出状态更新时出错", exc_info=True)

    return on_update


def _load_rules() -> RulesConfig | None:
    try:
        return load_rules_config(DEFAULT_RULES)
    except Exception as exc:
        print(f"规则配置 {DEFAULT_RULES} 有误：{exc}")
        return None


def _build_scheduler(cards, sync: bool, stats: Callable[[], MetaStats | None] | None = None):
    """按 config.toml 创建大模型调度器；不能用 AI 时打印原因并返回 None。
    config.toml 有误时抛出 ConfigError。stats：取本版本统计的函数（统计关闭时为 None）。"""
    from lscs.llm.prompt import load_guide
    from lscs.llm.provider import OpenAICompatProvider
    from lscs.llm.record import Recorder
    from lscs.llm.scheduler import LLMScheduler

    if not DEFAULT_CONFIG.exists():
        print(f"没有找到 {DEFAULT_CONFIG.absolute()}，只显示规则建议。")
        return None
    llm = load_config(DEFAULT_CONFIG).llm
    if not llm.enabled:
        print("AI 已在 config.toml 中关闭，只显示规则建议。")
        return None
    if not llm.api_key:
        print("config.toml 里没有填 api_key，只显示规则建议。")
        return None
    if cards is None:
        print("没有卡牌数据，无法使用 AI，只显示规则建议。")
        return None
    print(f"AI：{llm.model}（Key {mask_key(llm.api_key)}，思考模式 {llm.thinking}）", flush=True)
    if llm.deep_analysis:
        wait = deep_wait_hint(llm.deep_thinking)
        print(f"深度分析：开启（思考 {llm.deep_thinking}，每回合开始时{wait}出结果）", flush=True)
    else:
        print("深度分析：关闭", flush=True)
    if llm.thinking != "disabled":
        print(
            f"注意：快速建议开了思考模式 {llm.thinking}，会很慢（{deep_wait_hint(llm.thinking)}）；"
            "思考交给深度分析就好，thinking 建议改回 disabled。",
            flush=True,
        )
    low = []
    if llm.max_output_tokens < RECOMMENDED_OUTPUT_TOKENS:
        low.append(
            f"max_output_tokens = {llm.max_output_tokens}（推荐 {RECOMMENDED_OUTPUT_TOKENS}）"
        )
    if llm.max_calls_per_game < RECOMMENDED_CALLS_PER_GAME:
        low.append(
            f"max_calls_per_game = {llm.max_calls_per_game}（推荐 {RECOMMENDED_CALLS_PER_GAME}）"
        )
    if low:
        # 老版本的 config.toml 模板写死了这两个值，会盖掉后来提高的默认值
        print(
            "注意：config.toml 里的 " + "、".join(low) + " 比推荐值小，"
            "可能是旧模板留下的；删掉对应的行就会用推荐值。",
            flush=True,
        )
    if not llm.hero_advice:
        print("选英雄建议：关闭", flush=True)  # 开启时不打印，少一行
    if not llm.choice_advice:
        print("选项建议：关闭", flush=True)
    if not llm.repair_invalid:
        print("不合法方案自动改正：关闭", flush=True)
    if not llm.all_opponents:
        print("对手摘要：只发下一个对手", flush=True)
    heroes = load_guide(DEFAULT_HEROES) if DEFAULT_HEROES.exists() else None
    extra = {"debounce": 0.0, "hero_debounce": 0.0, "run_async": lambda job: job()} if sync else {}
    return LLMScheduler(
        OpenAICompatProvider(llm),
        llm,
        cards,
        load_guide(DEFAULT_GUIDE),
        heroes_note=heroes,
        recorder=Recorder(RECORDINGS_DIR),
        stats=stats,
        **extra,
    )


def _warn_old_highlight(cfg: HighlightConfig) -> None:
    """老版本的 config.toml 模板写死了高亮的颜色和校准之前的位置，会盖掉新的默认值。"""
    old = old_highlight_values(cfg)
    if old:
        print(
            "注意：config.toml 的 [highlight] 里还留着旧版的颜色、位置或尺寸（"
            + "、".join(old)
            + "），"
            "会盖掉新校准的值；删掉这些行（或整个 [highlight] 段）就会用新的默认值。",
            flush=True,
        )


def _overlay_config(explicit: bool) -> AppConfig | None:
    """读取 config.toml（没有文件时用默认值），面板要用其中的 [overlay] 和 [highlight]；
    有误时抛出 ConfigError。explicit 表示命令行明确要求打开（replay --overlay），此时不看
    [overlay] enabled。面板关闭时返回 None（也就没有画面高亮）。用到画面高亮时，[highlight]
    里还有旧模板的值就提示一次。"""
    cfg = load_config(DEFAULT_CONFIG)
    if not cfg.overlay.enabled and not explicit:
        print("悬浮面板已在 config.toml 中关闭，只在终端显示建议。")
        return None
    if cfg.highlight.enabled:
        _warn_old_highlight(cfg.highlight)
    return cfg


def _displays(
    name: Namer, overlay: AppConfig | None, scheduler, rules: RulesConfig | None = None
) -> tuple[OverlayModel | None, AdviceSink]:
    """终端总是显示；打开悬浮面板时，建议同时发给面板的数据模型。rules：面板按它的买牌价
    判断方案的第一步做不做得起（没有时按默认的 3 金）。"""
    deep_thinking = scheduler.deep_thinking if scheduler is not None else "low"
    terminal = AdviceDisplay(name, deep_thinking=deep_thinking)
    if overlay is None:
        return None, terminal
    ai = scheduler is not None
    model = OverlayModel(
        name,
        ai_enabled=ai,
        choice_ai=ai and scheduler.choice_advice,
        opponents=overlay.overlay.opponents,
        combat_result=overlay.combat_result.enabled,
        detail=overlay.overlay.detail,
        **({"buy_cost": rules.buy_cost_for} if rules is not None else {}),
    )
    return model, DisplayGroup(terminal, model)


def _run_overlay(
    work: overlay_app.Work, model: OverlayModel, cfg: AppConfig, finished_text: str
) -> None:
    window = functools.partial(
        overlay_app.create_tk_window,
        highlight=cfg.highlight,
        banner=cfg.combat_result.enabled,  # 棋盘顶部的战斗结果横幅画在高亮窗口上
    )
    overlay_app.run_with_overlay(
        work, model, cfg.overlay, create_window=window, finished_text=finished_text
    )


def _tracker(args: argparse.Namespace) -> StateTracker:
    return StateTracker(load_pool_config(Path(args.pool)))


def _review_config(explicit: bool = False) -> ReviewConfig | None:
    """config.toml 的 [review]（没有文件时用默认值）；关掉复盘时为 None。config.toml 有误时
    打印原因、这次不出复盘（建议照常）。explicit：lscs review 明确要复盘，不看 enabled。"""
    try:
        cfg = load_config(DEFAULT_CONFIG).review
    except ConfigError as exc:
        print(f"{exc}（这次不出复盘）")
        return None
    return cfg if cfg.enabled or explicit else None


def _positioning(cards) -> bool:
    """站位建议开不开：config.toml 的 [positioning] enabled（没有文件时用默认值），而且要有卡牌
    数据（亡语、进击、复仇都要看卡牌说明）。关掉、没有卡牌数据、config.toml 有误时打印一句。"""
    try:
        enabled = load_config(DEFAULT_CONFIG).positioning.enabled
    except ConfigError as exc:
        print(f"{exc}（这次不提示站位）", flush=True)
        return False
    if not enabled:
        print("站位建议：关闭", flush=True)
        return False
    if cards is None:
        print("站位建议：需要卡牌数据，本次关闭", flush=True)
        return False
    return True


def _review_dir(cfg: ReviewConfig) -> Path:
    path = Path(cfg.dir)
    return path if path.is_absolute() else REVIEW_BASE / path


class _LogClock:
    """回放日志时复盘用的时间：日期取日志文件名开头的 YYYY-MM-DD（没有时用文件的修改时间），
    时刻取最近读到的一行日志的时间戳（对局结束时就是结束的时刻）。"""

    _DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

    def __init__(self, path: Path) -> None:
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime)
        except OSError:
            modified = datetime.now()
        self._fallback = modified
        self._date = modified.date()
        match = self._DATE.match(path.name)
        if match:
            try:
                self._date = date(*(int(part) for part in match.groups()))
            except ValueError:
                pass
        self._seconds: float | None = None

    def track(self, lines: Iterable[str]) -> Iterator[str]:
        for line in lines:
            seconds = line_seconds(line)
            if seconds is not None:
                self._seconds = seconds
            yield line

    def __call__(self) -> datetime:
        if self._seconds is None:
            return datetime.combine(self._date, self._fallback.time())
        total = int(self._seconds) % 86400
        moment = day_time(total // 3600, total % 3600 // 60, total % 60)
        return datetime.combine(self._date, moment)


def _reviewer(
    cfg: ReviewConfig | None,
    rules: RulesConfig,
    name: Namer,
    display: AdviceSink,
    cards,
    scheduler,
    *,
    sync: bool,
    now: Callable[[], datetime] = datetime.now,
    save_history: bool = True,
) -> GameReviewer | None:
    """[review] 打开时创建复盘，它的显示端是终端（和面板）。有调度器（AI 开着）、[review] ai
    打开时，打完一局再调一次 AI 写复盘（用调度器的提供商和配置，不占每局的调用预算）。
    sync：replay 同步执行 AI 调用。"""
    if cfg is None:
        return None
    from lscs.llm.prompt import load_guide

    use_ai = scheduler is not None and cfg.ai
    extra = {"run_async": lambda job: job()} if sync else {}
    return GameReviewer(
        cfg,
        rules,
        name,
        display,
        cards,
        provider=scheduler.provider if use_ai else None,
        llm=scheduler.config if use_ai else None,
        guide=load_guide(DEFAULT_GUIDE) if use_ai else "",
        recorder=scheduler.recorder if use_ai else None,
        directory=_review_dir(cfg),
        base=REVIEW_BASE,
        now=now,
        save_history=save_history,
        # Key 连续无效、调度器已经关掉 AI 时不再为复盘调用
        ai_blocked=(lambda: scheduler.disabled_reason) if use_ai else (lambda: None),
        **extra,
    )


def _cmd_replay(args: argparse.Namespace) -> int:
    advice = args.advice or args.ai or args.overlay
    show = args.dump or advice
    overlay = None
    if args.overlay:
        try:
            overlay = _overlay_config(explicit=True)
        except ConfigError as exc:
            print(exc)
            return 1
    model = None
    handlers: list[UpdateHandler] = []
    clock = _LogClock(Path(args.log))
    # 面板是给人看的，预览时总是显示中文卡名
    cards = _load_cards() if (args.names or args.ai or args.overlay) and show else None
    name = cards.name if cards is not None else _identity
    if show:
        handlers.append(_timeline(name, args.verbose))
    if advice:
        rules = _load_rules()
        if rules is None:
            return 1
        stats = _stats_getter(_stats_store(sync=True))
        scheduler = None
        if args.ai:
            try:
                scheduler = _build_scheduler(cards, sync=True, stats=stats)
            except ConfigError as exc:
                print(exc)
                return 1
        model, display = _displays(name, overlay, scheduler, rules)
        # 回放的是录像：复盘照出，但不记进 history.jsonl（免得同一份录像反复算进去）
        reviewer = _reviewer(
            _review_config(),
            rules,
            name,
            display,
            cards,
            scheduler,
            sync=True,
            now=clock,
            save_history=False,
        )
        if reviewer is not None:
            display = DisplayGroup(display, reviewer.tap)
        runner = AdviceRunner(
            rules,
            name,
            display,
            scheduler,
            pool=_pool_estimator(cards, args),
            cards=cards,
            positioning=_positioning(cards),
            stats=stats,
        )
        if model is not None:
            handlers.append(model.on_update)  # 先更新面板的回合，再出建议
        handlers.append(runner.on_update)
        if scheduler is not None:
            handlers.append(lambda _state, _events: runner.tick())
        if reviewer is not None:
            # 在建议之后：本局 AI 用量那一行先打印，复盘在后面
            handlers.append(reviewer.on_update)
            handlers.append(lambda _state, _events: reviewer.tick())
    tracker = _tracker(args)
    updates = 0

    def count(_state: GameState, _events: list[Event]) -> None:
        nonlocal updates
        updates += 1

    pipeline = Pipeline(tracker, _combine(count, *handlers))
    speed = args.speed
    if model is not None and speed is None:
        speed = OVERLAY_REPLAY_SPEED

    def work(stop: threading.Event | None) -> None:
        push_paced(
            pipeline,
            clock.track(read_log_lines(Path(args.log))),
            speed,
            sleep=time.sleep if stop is None else stop.wait,
            interval=POLL_INTERVAL,
            stop=None if stop is None else stop.is_set,
        )

    if model is None or overlay is None:
        work(None)
    else:
        try:
            _run_overlay(work, model, overlay, finished_text="回放结束")
        except KeyboardInterrupt:
            pass
    if not show:
        print(f"回放完成：{updates} 次状态更新，最终阶段 {tracker.state.phase.value}。")
    return 0


def watch(
    tailer: LogTailer,
    pipeline: Pipeline,
    interval: float,
    max_polls: int | None = None,
    sleep=time.sleep,
    tick: Callable[[], None] | None = None,
    stop: Callable[[], bool] | None = None,
) -> None:
    """轮询日志直到 max_polls 次或 stop() 返回 True（悬浮面板模式下由 Ctrl+C 设置）。"""
    polls = 0
    announced = None
    while (max_polls is None or polls < max_polls) and not (stop is not None and stop()):
        lines: list[str] = []
        try:
            lines = tailer.poll()
        except OSError:
            log.warning("读取日志失败，稍后重试", exc_info=True)
        for line in lines:
            if stop is not None and stop():
                return  # 启动时一次可能读到几十万行，Ctrl+C 后不等它们推完
            pipeline.push(line)
        if tailer.path != announced:
            announced = tailer.path
            if announced is not None:
                print(f"正在读取 {announced}", flush=True)
        if not lines:
            # 一批日志写完（这次读不到新行）才更新状态，避免读到一半的半成品状态
            pipeline.flush()
        if tick is not None:
            tick()
        polls += 1
        sleep(interval)


def _cmd_watch(args: argparse.Namespace) -> int:
    install = _install_dir(args)
    if install is None:
        return 1
    rules = _load_rules()
    if rules is None:
        return 1
    try:
        overlay = None if args.no_overlay else _overlay_config(explicit=False)
        combat_result = load_config(DEFAULT_CONFIG).combat_result.enabled
    except ConfigError as exc:
        print(exc)
        return 1
    cards = _load_cards()
    name = cards.name if cards is not None else _identity
    stats = _stats_getter(_stats_store(sync=False))  # 卡牌数据之后、在后台：从不阻塞
    scheduler = None
    if not args.no_ai:
        try:
            scheduler = _build_scheduler(cards, sync=False, stats=stats)
        except ConfigError as exc:
            print(exc)
            return 1
    model, display = _displays(name, overlay, scheduler, rules)
    reviewer = _reviewer(_review_config(), rules, name, display, cards, scheduler, sync=False)
    if reviewer is not None:
        display = DisplayGroup(display, reviewer.tap)
    runner = AdviceRunner(
        rules,
        name,
        display,
        scheduler,
        pool=_pool_estimator(cards, args),
        cards=cards,
        positioning=_positioning(cards),
        stats=stats,
    )
    print(f"正在监听 {install / 'Logs'}，按 Ctrl+C 退出。")
    if combat_result:
        where = "棋盘顶部和这里" if overlay is not None else "这里"
        print(COMBAT_RESULT_NOTICE.format(where=where), flush=True)
    handlers = [_timeline(name, args.verbose, combat_result=combat_result)]
    if model is not None:
        handlers.append(model.on_update)  # 先更新面板的回合，再出建议
    handlers.append(runner.on_update)
    if reviewer is not None:
        handlers.append(reviewer.on_update)  # 在建议之后：本局 AI 用量那一行先打印
    tailer = LogTailer(lambda: latest_power_log(install))
    pipeline = Pipeline(_tracker(args), _combine(*handlers))

    def tick() -> None:
        runner.tick()
        if reviewer is not None:
            reviewer.tick()

    def work(stop: threading.Event | None) -> None:
        if stop is None:  # 只用终端：和以前一样，Ctrl+C 抛出 KeyboardInterrupt
            watch(tailer, pipeline, args.interval, tick=tick)
        else:  # 在后台线程里运行，面板模式的 Ctrl+C 只设置停止标志
            watch(tailer, pipeline, args.interval, tick=tick, stop=stop.is_set, sleep=stop.wait)

    try:
        if model is None or overlay is None:
            work(None)
        else:
            _run_overlay(work, model, overlay, finished_text=overlay_app.FINISHED_TEXT)
    except KeyboardInterrupt:
        pass
    return 0


def _print_habits(cfg: ReviewConfig) -> int:
    from lscs.review.flags import FIXES, KIND_NAMES, first_clause
    from lscs.review.report import HISTORY_FILE, MIN_HABIT_GAMES, habits, history_line, read_history

    path = _review_dir(cfg) / HISTORY_FILE
    entries = read_history(path)
    if not entries:
        print("还没有复盘记录（打完一局自动生成）")
        return 0
    recent = entries[-cfg.history_games :]
    print(f"最近 {len(recent)} 局（{path}）：")
    for entry in recent:
        print("  " + history_line(entry))
    habit = habits(entries, cfg.history_games)
    if habit is None:
        print(f"满 {MIN_HABIT_GAMES} 局才看习惯（现在 {len(entries)} 局）")
        return 0
    print(habit.line())
    for kind, count in habit.kinds:
        fix = first_clause(FIXES.get(kind, ""))
        print(f"  {KIND_NAMES.get(kind, kind)}（{count} 局）" + (f"→ {fix}" if fix else ""))
    return 0


def _cmd_review(args: argparse.Namespace) -> int:
    """给录像出复盘：和 replay --advice（--ai）一样回放，但不打时间线和每回合的建议，只打复盘；
    --habits 只看 history.jsonl 里最近几局的习惯。"""
    cfg = _review_config(explicit=True)
    if cfg is None:
        return 1
    if args.habits:
        return _print_habits(cfg)
    if not args.log:
        print("请给出要复盘的日志文件（Power.log 或 .log.gz），或者用 --habits 看最近几局的习惯。")
        return 1
    log_path = Path(args.log)
    if not log_path.exists():
        print(f"找不到 {log_path}")
        return 1
    rules = _load_rules()
    if rules is None:
        return 1
    cards = _load_cards()  # 复盘是给人看的：总是显示中文卡名
    name = cards.name if cards is not None else _identity
    stats = _stats_getter(_stats_store(sync=True))
    scheduler = None
    if args.ai:
        try:
            scheduler = _build_scheduler(cards, sync=True, stats=stats)
        except ConfigError as exc:
            print(exc)
            return 1
        cfg = cfg.model_copy(update={"ai": True})  # 命令行明确要 AI
    deep_thinking = scheduler.deep_thinking if scheduler is not None else "low"
    terminal = AdviceDisplay(name, deep_thinking=deep_thinking)
    clock = _LogClock(log_path)
    reviewer = _reviewer(
        cfg,
        rules,
        name,
        terminal,
        cards,
        scheduler,
        sync=True,
        now=clock,
        save_history=args.save_history,
    )
    assert reviewer is not None
    # 建议只交给复盘记下来，不打印
    runner = AdviceRunner(
        rules, name, reviewer.tap, scheduler, pool=_pool_estimator(cards, args), stats=stats
    )
    handlers: list[UpdateHandler] = [runner.on_update]
    if scheduler is not None:
        handlers.append(lambda _state, _events: runner.tick())
    handlers += [reviewer.on_update, lambda _state, _events: reviewer.tick()]
    pipeline = Pipeline(_tracker(args), _combine(*handlers))
    push_paced(
        pipeline,
        clock.track(read_log_lines(log_path)),
        None,
        sleep=time.sleep,
        interval=POLL_INTERVAL,
    )
    reviewer.tick()
    if reviewer.last_review is None and not reviewer.already_done:
        print("这份日志里没有打完的对局，没有复盘。")
    return 0


def _cmd_overlay_check(args: argparse.Namespace) -> int:
    from lscs.overlay import check

    try:
        cfg = load_config(DEFAULT_CONFIG).overlay  # 只用 [overlay]（不透明度、是否穿透）
    except ConfigError as exc:
        print(exc)
        return 1
    return 0 if check.run_check(cfg) else 1


def _rows(text: str) -> list[str]:
    """highlight-test 的 --rows：逗号分隔的排名。"""
    from lscs.overlay.layout import CALIBRATION_ROWS

    rows = [part.strip() for part in text.split(",") if part.strip()]
    unknown = [row for row in rows if row not in CALIBRATION_ROWS]
    if not rows or unknown:
        raise argparse.ArgumentTypeError(
            f"可选的排：{','.join(CALIBRATION_ROWS)}（不认识：{','.join(unknown) or '空'}）"
        )
    return rows


def _seconds(text: str) -> float:
    value = float(text)
    if not 0 < value <= 600:
        raise argparse.ArgumentTypeError("应为 0~600 秒")
    return value


def _cmd_highlight_test(args: argparse.Namespace) -> int:
    from lscs.overlay import calibrate
    from lscs.overlay.layout import DEFAULT_CALIBRATION_ROWS

    try:
        cfg = load_config(DEFAULT_CONFIG).highlight  # 只用 [highlight]；没有文件时用默认值
    except ConfigError as exc:
        print(exc)
        return 1
    _warn_old_highlight(cfg)  # 不看 enabled：highlight-test 照样用这些位置画框
    try:
        rows = args.rows or DEFAULT_CALIBRATION_ROWS
        return 0 if calibrate.run_highlight_test(cfg, args.seconds, rows) else 1
    except KeyboardInterrupt:
        return 0


def _setup_output() -> None:
    # Windows 中文系统重定向输出时默认编码是 GBK，打印 ❄ 等字符会抛异常；改为替换无法编码的字符
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(errors="replace")


def _add_output_options(p: argparse.ArgumentParser, names_flag: bool = True) -> None:
    if names_flag:
        p.add_argument(
            "--names", action="store_true", help="显示中文卡名（首次使用会下载卡牌数据）"
        )
    else:
        # watch 总是显示中文卡名；保留这个参数，兼容以前的用法
        p.add_argument("--names", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--verbose", action="store_true", help="显示所有事件")
    p.add_argument("--pool", default=str(DEFAULT_POOL), help="卡池配置文件")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lscs", description="酒馆战棋 AI 助手")
    parser.add_argument("--version", action="version", version=f"lscs {__version__}")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("fix-log-config", help="检查并修复炉石的 log.config")
    p.add_argument("--path", help="log.config 路径（默认自动判断）")
    p.add_argument("--install-dir", help="炉石安装目录，用于写入 client.config（默认自动探测）")
    p.set_defaults(func=_cmd_fix_log_config)

    p = sub.add_parser("find-logs", help="列出炉石的 Power.log")
    p.add_argument("--install-dir", help="炉石安装目录（默认自动探测）")
    p.set_defaults(func=_cmd_find_logs)

    p = sub.add_parser("replay", help="回放录像并打印状态时间线")
    p.add_argument("log", help="Power.log 或 .log.gz 文件")
    p.add_argument("--speed", type=float, default=None, help="回放倍速；不填则不等待")
    p.add_argument("--dump", action="store_true", help="打印状态时间线")
    p.add_argument("--advice", action="store_true", help="在时间线中附加规则建议（隐含 --dump）")
    p.add_argument(
        "--ai",
        action="store_true",
        help="同时真的调用大模型（隐含 --advice，计入预算、花费 token）",
    )
    p.add_argument(
        "--overlay",
        action="store_true",
        help=f"同时打开悬浮面板预览（隐含 --advice；--speed 默认 {OVERLAY_REPLAY_SPEED:g}）",
    )
    _add_output_options(p)
    p.set_defaults(func=_cmd_replay)

    p = sub.add_parser("watch", help="实时监听炉石日志，打印状态时间线和建议")
    p.add_argument("--install-dir", help="炉石安装目录（默认自动探测）")
    p.add_argument("--interval", type=float, default=POLL_INTERVAL, help="轮询间隔（秒）")
    p.add_argument("--no-ai", action="store_true", help="只显示规则建议，不调用大模型")
    p.add_argument("--no-overlay", action="store_true", help="不打开游戏上的悬浮面板，只在终端显示")
    _add_output_options(p, names_flag=False)
    p.set_defaults(func=_cmd_watch)

    p = sub.add_parser("review", help="给打完的对局出复盘；--habits 看最近几局的习惯")
    p.add_argument("log", nargs="?", help="Power.log 或 .log.gz 文件")
    p.add_argument(
        "--ai",
        action="store_true",
        help="同时让 AI 写复盘（会把整局的 AI 建议也重放一遍：计入预算、花费 token）",
    )
    p.add_argument(
        "--save-history",
        action="store_true",
        help="把这局记进 history.jsonl（默认不记，免得同一份录像反复算进去）",
    )
    p.add_argument("--habits", action="store_true", help="只看 history.jsonl 里最近几局的习惯")
    # 复盘总是显示中文卡名；保留这个参数，和 replay 的用法一致
    p.add_argument("--names", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--pool", default=str(DEFAULT_POOL), help="卡池配置文件")
    p.set_defaults(func=_cmd_review)

    p = sub.add_parser(
        "overlay-check", help="检查悬浮面板能否贴靠炉石、穿透鼠标（Windows，先打开炉石）"
    )
    p.set_defaults(func=_cmd_overlay_check)

    p = sub.add_parser(
        "highlight-test",
        help="在炉石画面上画出高亮的位置（默认商店、场面和按钮），截图核对位置（Windows，先打开炉石）",
    )
    p.add_argument("--seconds", type=_seconds, default=10.0, help="显示几秒后自动关闭")
    p.add_argument(
        "--rows",
        type=_rows,
        default=None,
        help="只画这几排，逗号分隔：shop,board,choice,choice4,hero,buttons（默认商店、场面、按钮）",
    )
    p.set_defaults(func=_cmd_highlight_test)
    return parser


def _setup_logging() -> None:
    # hslog 直接用根记录器打印大量无害警告（如 "Broken option nesting"），这里只放行错误；
    # 本项目自己的警告走 "lscs" 记录器，照常显示。
    logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("lscs").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    _setup_output()
    _setup_logging()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    return args.func(args)
