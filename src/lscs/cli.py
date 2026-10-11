"""命令行入口。"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from lscs import __version__
from lscs.advisor.display import AdviceDisplay, AdviceSink, DisplayGroup, deep_wait_hint
from lscs.advisor.rules_config import RulesConfig, load_rules_config
from lscs.advisor.runner import AdviceRunner
from lscs.config import DEFAULT_CONFIG, ConfigError, OverlayConfig, load_config, mask_key
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
from lscs.replay import push_paced, read_log_lines
from lscs.state.models import Event, GameState
from lscs.state.pool import load_pool_config
from lscs.state.tracker import StateTracker

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POOL = PROJECT_ROOT / "strategy" / "current" / "pool.toml"
DEFAULT_RULES = PROJECT_ROOT / "strategy" / "current" / "rules.toml"
DEFAULT_GUIDE = PROJECT_ROOT / "strategy" / "current" / "guide.md"
RECORDINGS_DIR = PROJECT_ROOT / "recordings"
POLL_INTERVAL = 0.2  # watch 默认的轮询间隔（秒）
OVERLAY_REPLAY_SPEED = 20.0  # replay --overlay 没填 --speed 时的倍速（不等待的话面板一闪而过）
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


def _namer(args: argparse.Namespace) -> Namer:
    if not args.names:
        return _identity
    cards = _load_cards()
    return cards.name if cards is not None else _identity


def _timeline(name: Namer, verbose: bool) -> UpdateHandler:
    def on_update(state: GameState, events: list[Event]) -> None:
        for line in describe(state, events, name, verbose=verbose):
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


def _build_scheduler(cards, sync: bool):
    """按 config.toml 创建大模型调度器；不能用 AI 时打印原因并返回 None。
    config.toml 有误时抛出 ConfigError。"""
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
    extra = {"debounce": 0.0, "run_async": lambda job: job()} if sync else {}
    return LLMScheduler(
        OpenAICompatProvider(llm),
        llm,
        cards,
        load_guide(DEFAULT_GUIDE),
        recorder=Recorder(RECORDINGS_DIR),
        **extra,
    )


def _overlay_config(explicit: bool) -> OverlayConfig | None:
    """读取 config.toml 的 [overlay]（没有文件时用默认值）；有误时抛出 ConfigError。
    explicit 表示命令行明确要求打开（replay --overlay），此时不看 enabled。"""
    cfg = load_config(DEFAULT_CONFIG).overlay
    if not cfg.enabled and not explicit:
        print("悬浮面板已在 config.toml 中关闭，只在终端显示建议。")
        return None
    return cfg


def _displays(
    name: Namer, overlay: OverlayConfig | None, scheduler
) -> tuple[OverlayModel | None, AdviceSink]:
    """终端总是显示；打开悬浮面板时，建议同时发给面板的数据模型。"""
    deep_thinking = scheduler.deep_thinking if scheduler is not None else "low"
    terminal = AdviceDisplay(name, deep_thinking=deep_thinking)
    if overlay is None:
        return None, terminal
    model = OverlayModel(name, ai_enabled=scheduler is not None)
    return model, DisplayGroup(terminal, model)


def _run_overlay(
    work: overlay_app.Work, model: OverlayModel, cfg: OverlayConfig, finished_text: str
) -> None:
    overlay_app.run_with_overlay(
        work,
        model,
        cfg,
        create_window=overlay_app.create_tk_window,
        finished_text=finished_text,
    )


def _tracker(args: argparse.Namespace) -> StateTracker:
    return StateTracker(load_pool_config(Path(args.pool)))


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
    # 面板是给人看的，预览时总是显示中文卡名
    cards = _load_cards() if (args.names or args.ai or args.overlay) and show else None
    name = cards.name if cards is not None else _identity
    if show:
        handlers.append(_timeline(name, args.verbose))
    if advice:
        rules = _load_rules()
        if rules is None:
            return 1
        scheduler = None
        if args.ai:
            try:
                scheduler = _build_scheduler(cards, sync=True)
            except ConfigError as exc:
                print(exc)
                return 1
        model, display = _displays(name, overlay, scheduler)
        runner = AdviceRunner(rules, name, display, scheduler)
        if model is not None:
            handlers.append(model.on_update)  # 先更新面板的回合，再出建议
        handlers.append(runner.on_update)
        if scheduler is not None:
            handlers.append(lambda _state, _events: runner.tick())
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
            read_log_lines(Path(args.log)),
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
    except ConfigError as exc:
        print(exc)
        return 1
    cards = _load_cards()
    name = cards.name if cards is not None else _identity
    scheduler = None
    if not args.no_ai:
        try:
            scheduler = _build_scheduler(cards, sync=False)
        except ConfigError as exc:
            print(exc)
            return 1
    model, display = _displays(name, overlay, scheduler)
    runner = AdviceRunner(rules, name, display, scheduler)
    print(f"正在监听 {install / 'Logs'}，按 Ctrl+C 退出。")
    handlers = [_timeline(name, args.verbose)]
    if model is not None:
        handlers.append(model.on_update)  # 先更新面板的回合，再出建议
    handlers.append(runner.on_update)
    tailer = LogTailer(lambda: latest_power_log(install))
    pipeline = Pipeline(_tracker(args), _combine(*handlers))

    def work(stop: threading.Event | None) -> None:
        if stop is None:  # 只用终端：和以前一样，Ctrl+C 抛出 KeyboardInterrupt
            watch(tailer, pipeline, args.interval, tick=runner.tick)
        else:  # 在后台线程里运行，面板模式的 Ctrl+C 只设置停止标志
            watch(
                tailer, pipeline, args.interval, tick=runner.tick, stop=stop.is_set, sleep=stop.wait
            )

    try:
        if model is None or overlay is None:
            work(None)
        else:
            _run_overlay(work, model, overlay, finished_text=overlay_app.FINISHED_TEXT)
    except KeyboardInterrupt:
        pass
    return 0


def _cmd_overlay_check(args: argparse.Namespace) -> int:
    from lscs.overlay import check

    try:
        cfg = load_config(DEFAULT_CONFIG).overlay  # 只用 [overlay]（不透明度、是否穿透）
    except ConfigError as exc:
        print(exc)
        return 1
    return 0 if check.run_check(cfg) else 1


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

    p = sub.add_parser(
        "overlay-check", help="检查悬浮面板能否贴靠炉石、穿透鼠标（Windows，先打开炉石）"
    )
    p.set_defaults(func=_cmd_overlay_check)
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
