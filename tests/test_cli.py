import pytest
from helpers import NEEDS_USER_LOG

from lscs import __version__
from lscs.cli import main


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "酒馆战棋" in capsys.readouterr().out


def test_fix_log_config_command(tmp_path, capsys):
    path = tmp_path / "log.config"
    assert main(["fix-log-config", "--path", str(path)]) == 0
    assert "已写入" in capsys.readouterr().out
    assert main(["fix-log-config", "--path", str(path)]) == 0
    assert "无需修改" in capsys.readouterr().out


def test_find_logs_command(tmp_path, capsys):
    assert main(["find-logs", "--install-dir", str(tmp_path / "missing")]) == 1
    assert "找不到炉石安装目录" in capsys.readouterr().out
    assert main(["find-logs", "--install-dir", str(tmp_path)]) == 1
    log = tmp_path / "Logs" / "Hearthstone_2026_10_10_10_00_00" / "Power.log"
    log.parent.mkdir(parents=True)
    log.write_text("x")
    capsys.readouterr()
    assert main(["find-logs", "--install-dir", str(tmp_path)]) == 0
    assert str(log) in capsys.readouterr().out


def test_replay_command_prints_timeline(capsys):
    from helpers import MINI_LOG

    assert main(["replay", str(MINI_LOG), "--dump"]) == 0
    out = capsys.readouterr().out
    assert "[T 1] 招募开始" in out
    assert "[T 2] 对局结束 名次 5" in out


def test_replay_without_dump_prints_summary(capsys):
    from helpers import MINI_LOG

    assert main(["replay", str(MINI_LOG)]) == 0
    assert capsys.readouterr().out == "回放完成：11 次状态更新，最终阶段 GAME_OVER。\n"


def test_watch_loop_reads_new_lines(tmp_path, mini_lines):
    from helpers import TEST_POOL

    from lscs.cli import watch
    from lscs.logwatch.tail import LogTailer
    from lscs.pipeline import Pipeline
    from lscs.state.tracker import StateTracker

    log = tmp_path / "Power.log"
    log.write_text("\n".join(mini_lines[:60]) + "\n", encoding="utf-8")
    updates = []
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: updates.append((s, e)))

    appended = []

    def append_rest(_interval):
        if not appended:
            with log.open("a", encoding="utf-8") as f:
                f.write("\n".join(mini_lines[60:]) + "\n")
            appended.append(True)

    # 第 3 次轮询读不到新行，此时才 flush 最后一组
    watch(LogTailer(lambda: log), pipeline, interval=0, max_polls=3, sleep=append_rest)
    assert updates[-1][0].final_place == 5


def test_watch_survives_os_error(mini_lines):
    from helpers import TEST_POOL

    from lscs.cli import watch
    from lscs.pipeline import Pipeline
    from lscs.state.tracker import StateTracker

    poll_count = [0]

    class FakeTailer:
        path = None

        def poll(self):
            poll_count[0] += 1
            if poll_count[0] == 1:
                raise PermissionError("no access")
            return mini_lines if poll_count[0] == 2 else []

    updates = []
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: updates.append((s, e)))
    watch(FakeTailer(), pipeline, interval=0, max_polls=3, sleep=lambda _: None)
    assert updates[-1][0].final_place == 5


def test_setup_output_replaces_unencodable_chars():
    import io
    import sys

    from lscs.cli import _setup_output

    original_stdout = sys.stdout
    try:
        sys.stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
        _setup_output()
        print("❄")
        sys.stdout.flush()
        sys.stdout.buffer.seek(0)
        assert b"?" in sys.stdout.buffer.read()
    finally:
        sys.stdout = original_stdout


def test_fix_log_config_also_writes_client_config(tmp_path, capsys):
    path = tmp_path / "log.config"
    install = tmp_path / "Hearthstone"
    install.mkdir()
    args = ["fix-log-config", "--path", str(path), "--install-dir", str(install)]
    assert main(args) == 0
    assert "FileSizeLimit.Int=-1" in (install / "client.config").read_text(encoding="utf-8")
    assert "client.config" in capsys.readouterr().out
    assert main(args) == 0
    assert capsys.readouterr().out.count("无需修改") == 2


def test_fix_log_config_missing_install_dir_warns(tmp_path, capsys):
    path = tmp_path / "log.config"
    args = ["fix-log-config", "--path", str(path), "--install-dir", str(tmp_path / "nope")]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "--install-dir" in out
    assert path.exists()


def test_fix_log_config_client_config_permission_error(tmp_path, capsys, monkeypatch):
    import lscs.cli

    def deny(_install):
        raise PermissionError("denied")

    monkeypatch.setattr(lscs.cli, "fix_client_config", deny)
    install = tmp_path / "Hearthstone"
    install.mkdir()
    args = ["fix-log-config", "--path", str(tmp_path / "log.config"), "--install-dir", str(install)]
    assert main(args) == 0
    out = capsys.readouterr().out
    assert "管理员" in out
    assert "FileSizeLimit.Int=-1" in out


def test_namer_falls_back_to_ids_when_card_data_unavailable(monkeypatch, capsys):
    import argparse

    import lscs.cards.fetch
    from lscs.cli import _namer

    def boom(_cache_dir):
        raise OSError("offline")

    monkeypatch.setattr(lscs.cards.fetch, "ensure_cards_json", boom)
    name = _namer(argparse.Namespace(names=True))
    assert name("BG21_029") == "BG21_029"
    out = capsys.readouterr().out
    assert "正在准备卡牌数据" in out
    assert "卡牌数据" in out.split("\n", 1)[1]  # 警告


def test_watch_announces_log_path_and_switches(tmp_path, capsys):
    from helpers import TEST_POOL

    from lscs.cli import watch
    from lscs.pipeline import Pipeline
    from lscs.state.tracker import StateTracker

    a, b = tmp_path / "a" / "Power.log", tmp_path / "b" / "Power.log"
    for p in (a, b):
        p.parent.mkdir()
        p.write_text("")
    targets = iter([a, a, b])

    from lscs.logwatch.tail import LogTailer

    tailer = LogTailer(lambda: next(targets))
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: None)
    watch(tailer, pipeline, interval=0, max_polls=3, sleep=lambda _: None)
    out = capsys.readouterr().out
    assert out.count("正在读取") == 2
    assert f"正在读取 {a}" in out and f"正在读取 {b}" in out


def test_watch_flushes_only_after_a_quiet_poll(mini_lines):
    from helpers import TEST_POOL

    from lscs.cli import watch
    from lscs.pipeline import Pipeline
    from lscs.state.tracker import StateTracker

    first_group = [line for line in mini_lines if line.split(" ")[1] == "10:00:00.0000000"]
    batches = iter([first_group[:10], first_group[10:], []])

    class FakeTailer:
        path = None

        def poll(self):
            return next(batches)

    updates = []
    seen = []
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: updates.append((s, e)))
    watch(
        FakeTailer(),
        pipeline,
        interval=0,
        max_polls=3,
        sleep=lambda _: None,
        tick=lambda: seen.append(len(updates)),
    )
    # 前两次轮询读到的是同一个时间戳组的前后两半，都不 flush；第 3 次读不到新行才 flush
    assert seen == [0, 0, 1]


def test_replay_with_advice_prints_rules(capsys):
    from helpers import MINI_LOG

    assert main(["replay", str(MINI_LOG), "--advice"]) == 0
    out = capsys.readouterr().out
    assert "[T 1] 招募开始" in out
    assert "  📋 规则：① 买 BGS_116 1/4（商店里身材最好，有嘲讽）" in out


def test_replay_with_broken_rules_file_fails(monkeypatch, tmp_path, capsys):
    from helpers import MINI_LOG

    import lscs.cli

    bad = tmp_path / "rules.toml"
    bad.write_text("[curve]\n", encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_RULES", bad)
    assert main(["replay", str(MINI_LOG), "--advice"]) == 1
    assert "规则配置" in capsys.readouterr().out


# mini 录像的选英雄阶段只有一个备选英雄（实体 14，米尔豪斯）
MINI_HERO = '{"pick": 14, "reason": "只有一个可选"}'


def _ai_cli_setup(monkeypatch, tmp_path, answers, key="sk-cli-0123456789abcd", extra=""):
    from helpers import FIXTURES

    import lscs.cli
    import lscs.llm.provider
    from lscs.cards.db import CardDB
    from lscs.llm.provider import FakeProvider

    config = tmp_path / "config.toml"
    config.write_text(f'[llm]\napi_key = "{key}"\n{extra}', encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(lscs.cli, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(lscs.cli, "DEFAULT_HEROES", tmp_path / "no-heroes.md")
    monkeypatch.setattr(
        lscs.cli, "_load_cards", lambda: CardDB.from_json_file(FIXTURES / "cards_mini.json")
    )
    fake = FakeProvider(answers)
    monkeypatch.setattr(lscs.llm.provider, "OpenAICompatProvider", lambda cfg: fake)
    return key


def test_replay_with_ai_calls_model_and_hides_key(monkeypatch, tmp_path, capsys):
    import json

    from helpers import MINI_LOG

    answer = json.dumps({"actions": [{"type": "buy", "entity_id": 31, "reason": "海盗"}]})
    key = _ai_cli_setup(monkeypatch, tmp_path, [MINI_HERO] + [answer] * 6)
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    out = capsys.readouterr().out
    assert "AI：deepseek-flash（Key sk-c…abcd，思考模式 disabled）" in out
    assert "深度分析：开启（思考 low，每回合开始时约 20 秒出结果）" in out
    assert "建议改回 disabled" not in out and "选英雄建议" not in out  # 开启时不多打印一行
    assert "自动改正" not in out
    # 选英雄：规则马上出，AI 在"选择英雄"之前出；耗时取决于机器，不断言
    hero = out.index("  🤖 AI（")
    assert out.index("  📋 规则：护甲信息不全，等 AI 建议") < hero < out.index("选择英雄")
    assert out[hero:].split("\n", 1)[0].endswith("：选 米尔豪斯·法力风暴 —— 只有一个可选")
    # 每个招募回合快速、深度各一次
    assert out.count("：① 买 海盗无赖 3/1（海盗）") == 2
    assert "  🧠 深度分析中…（约 20 秒）" in out and "  🧠 深度分析（" in out
    # 第 2 回合 #31 已经在场上，同样的回答不合法；改正一次还是同样的回答
    assert "  🤖 AI 建议不合法（第 1 步：商店里没有 #31），正在让它改正…" in out
    assert "  🧠 深度分析的操作不合法（第 1 步：商店里没有 #31），正在让它改正…" in out
    assert "  🤖 AI 改正后仍不合法，已忽略" in out
    assert "  🧠 深度分析失败：AI 改正后仍不合法，已忽略" in out
    assert "本局 AI 调用 7 次" in out
    assert key not in out
    record = next((tmp_path / "recordings").glob("llm-*.jsonl")).read_text(encoding="utf-8")
    assert key not in record and record.count("\n") == 7
    rows = [json.loads(line) for line in record.splitlines()]
    assert [(r["lane"], r["repair"]) for r in rows] == [
        ("hero", False),
        ("fast", False),
        ("deep", False),
        ("fast", False),
        ("fast", True),
        ("deep", False),
        ("deep", True),
    ]


def test_deep_thinking_high_waits_longer(monkeypatch, tmp_path, capsys):
    from helpers import MINI_LOG

    _ai_cli_setup(monkeypatch, tmp_path, [], extra='deep_thinking = "high"\n')
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    out = capsys.readouterr().out
    assert "深度分析：开启（思考 high，每回合开始时约 40 秒出结果）" in out
    assert "  🧠 深度分析中…（约 40 秒）" in out and "约 20 秒" not in out


def test_replay_with_deep_analysis_off(monkeypatch, tmp_path, capsys):
    import json

    from helpers import MINI_LOG

    answer = json.dumps({"actions": [{"type": "buy", "entity_id": 31, "reason": "海盗"}]})
    _ai_cli_setup(
        monkeypatch, tmp_path, [MINI_HERO] + [answer] * 3, extra="deep_analysis = false\n"
    )
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    out = capsys.readouterr().out
    assert "深度分析：关闭" in out and "🧠" not in out
    # 选英雄 1 次 + 两个招募回合各 1 次 + 第 2 回合的回答不合法，改正 1 次
    assert "本局 AI 调用 4 次" in out


def test_replay_with_hero_advice_off(monkeypatch, tmp_path, capsys):
    import json

    from helpers import MINI_LOG

    answer = json.dumps({"actions": [{"type": "buy", "entity_id": 31, "reason": "海盗"}]})
    extra = "deep_analysis = false\nhero_advice = false\n"
    _ai_cli_setup(monkeypatch, tmp_path, [answer] * 3, extra=extra)
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    out = capsys.readouterr().out
    assert "选英雄建议：关闭" in out and "选项建议" not in out
    assert "  📋 规则：护甲信息不全，等 AI 建议" in out  # 规则建议照常
    assert "本局 AI 调用 3 次" in out  # 第 2 回合的回答不合法，改正 1 次
    record = next((tmp_path / "recordings").glob("llm-*.jsonl")).read_text(encoding="utf-8")
    assert [json.loads(line)["lane"] for line in record.splitlines()] == ["fast"] * 3


def test_replay_with_choice_advice_off(monkeypatch, tmp_path, capsys):
    from helpers import MINI_LOG

    _ai_cli_setup(monkeypatch, tmp_path, [], extra="choice_advice = false\n")
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    assert "选项建议：关闭" in capsys.readouterr().out


def test_replay_with_repair_off(monkeypatch, tmp_path, capsys):
    import json

    from helpers import MINI_LOG

    answer = json.dumps({"actions": [{"type": "buy", "entity_id": 31, "reason": "海盗"}]})
    extra = "deep_analysis = false\nrepair_invalid = false\n"
    _ai_cli_setup(monkeypatch, tmp_path, [MINI_HERO] + [answer] * 2, extra=extra)
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    out = capsys.readouterr().out
    assert "不合法方案自动改正：关闭" in out
    assert "  🤖 AI 建议无效，已忽略" in out
    assert out.count("改正") == 1  # 只有启动时那一行
    assert "本局 AI 调用 3 次" in out


def test_replay_with_ai_asks_the_choice_lane(monkeypatch, tmp_path, capsys):
    import json

    from helpers import FIXTURES

    pick = json.dumps({"pick": 61, "reason": "先加攻击"}, ensure_ascii=False)
    hero = json.dumps({"actions": []})
    _ai_cli_setup(monkeypatch, tmp_path, [pick] + [hero] * 8, extra="deep_analysis = false\n")
    assert main(["replay", str(FIXTURES / "power" / "choice.log"), "--ai"]) == 0
    out = capsys.readouterr().out
    offered = out.index("[T 1] 选择 BG21_HERO_000p（3 选 1）")
    answer = out.index("：选 左数第 2 个 BG21_HERO_000_Buddyt —— 先加攻击")
    assert offered < answer < out.index("[T 1] 招募开始")  # 在 MAIN_ACTION 之前就给出
    record = next((tmp_path / "recordings").glob("llm-*.jsonl")).read_text(encoding="utf-8")
    rows = [json.loads(line) for line in record.splitlines()]
    assert rows[0]["lane"] == "choice"
    # 5 组选项各一个请求（后 4 组的回答没有 pick，各改正一次）
    assert len({r["request_id"] for r in rows if r["lane"] == "choice"}) == 5


def test_replay_sends_heroes_md_to_the_hero_lane(monkeypatch, tmp_path, capsys):
    from helpers import MINI_LOG

    import lscs.cli
    import lscs.llm.provider
    from lscs.llm.provider import FakeProvider

    _ai_cli_setup(monkeypatch, tmp_path, [], extra="deep_analysis = false\n")
    fake = FakeProvider([MINI_HERO])
    monkeypatch.setattr(lscs.llm.provider, "OpenAICompatProvider", lambda cfg: fake)
    heroes = tmp_path / "heroes.md"
    heroes.write_text("2026-10-20 英雄强度资料\n", encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_HEROES", heroes)
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    assert fake.calls[0].system.endswith("【本月英雄强度资料】\n2026-10-20 英雄强度资料")
    assert "英雄强度资料" not in fake.calls[1].system  # 招募阶段的系统提示不变


def test_replay_with_ai_and_broken_config_fails(monkeypatch, tmp_path, capsys):
    from helpers import MINI_LOG

    import lscs.cli

    config = tmp_path / "config.toml"
    config.write_text("[llm]\ntimeout_seconds = -1\n", encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(lscs.cli, "_load_cards", lambda: None)
    assert main(["replay", str(MINI_LOG), "--ai"]) == 1
    assert "llm.timeout_seconds" in capsys.readouterr().out


def test_replay_with_ai_without_config_file_uses_rules_only(monkeypatch, tmp_path, capsys):
    from helpers import MINI_LOG

    import lscs.cli

    missing = tmp_path / "config.toml"
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", missing)
    monkeypatch.setattr(lscs.cli, "_load_cards", lambda: None)
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    out = capsys.readouterr().out
    assert f"没有找到 {missing.absolute()}，只显示规则建议。" in out
    assert "api_key" not in out
    assert "📋 规则" in out and "🤖" not in out


def test_replay_with_ai_without_key_uses_rules_only(monkeypatch, tmp_path, capsys):
    from helpers import MINI_LOG

    import lscs.cli

    config = tmp_path / "config.toml"
    config.write_text('[llm]\napi_key = ""\n', encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(lscs.cli, "_load_cards", lambda: None)
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    out = capsys.readouterr().out
    assert "config.toml 里没有填 api_key，只显示规则建议。" in out
    assert "没有找到" not in out
    assert "📋 规则" in out and "🤖" not in out


def test_thinking_mode_prints_a_warning(monkeypatch, tmp_path, capsys):
    from helpers import MINI_LOG

    _ai_cli_setup(monkeypatch, tmp_path, [], extra='thinking = "low"\n')
    assert main(["replay", str(MINI_LOG), "--ai"]) == 0
    out = capsys.readouterr().out
    assert "思考模式 low" in out
    assert "建议改回 disabled" in out


def test_combine_isolates_each_handler(caplog):
    from lscs.cli import _combine

    seen = []

    def boom(_state, _events):
        raise RuntimeError("时间线坏了")

    _combine(boom, lambda s, e: seen.append(s))("状态", [])
    assert seen == ["状态"]
    assert "时间线坏了" in caplog.text


def test_watch_accepts_no_ai_flag():
    from lscs.cli import build_parser

    assert build_parser().parse_args(["watch", "--no-ai"]).no_ai is True
    assert build_parser().parse_args(["watch"]).no_ai is False


def _catch_up(lines):
    """模拟 watch 启动时从头读到 lines：第一次轮询读到全部，之后没有新行。
    轮询间隔 0.2 秒（假时钟）：一共 5 次轮询，选英雄等的 0.5 秒会过去，刷新等的 1 秒不会。"""
    from helpers import FIXTURES

    from lscs.advisor.display import AdviceDisplay
    from lscs.advisor.rules_config import load_rules_config
    from lscs.advisor.runner import AdviceRunner
    from lscs.cards.db import CardDB
    from lscs.cli import DEFAULT_POOL, DEFAULT_RULES, watch
    from lscs.config import LLMConfig
    from lscs.llm.provider import FakeProvider
    from lscs.llm.scheduler import LLMScheduler
    from lscs.pipeline import Pipeline
    from lscs.state.pool import load_pool_config
    from lscs.state.tracker import StateTracker

    cards = CardDB.from_json_file(FIXTURES / "cards_mini.json")
    jobs, out = [], []
    now = [1000.0]
    provider = FakeProvider([])
    scheduler = LLMScheduler(
        provider,
        LLMConfig(api_key="sk-x-123456789"),
        cards,
        guide="",
        run_async=jobs.append,
        clock=lambda: now[0],
    )
    runner = AdviceRunner(
        load_rules_config(DEFAULT_RULES),
        cards.name,
        AdviceDisplay(cards.name, out.append),
        scheduler,
    )
    batches = iter([lines])

    class FakeTailer:
        path = None

        def poll(self):
            return next(batches, [])

    pipeline = Pipeline(StateTracker(load_pool_config(DEFAULT_POOL)), runner.on_update)

    def sleep(seconds):
        now[0] += seconds

    watch(FakeTailer(), pipeline, interval=0.2, max_polls=5, sleep=sleep, tick=runner.tick)
    return jobs, provider, out


@NEEDS_USER_LOG
def test_watch_catch_up_after_a_finished_game_calls_nothing():
    from helpers import USER_LOG

    from lscs.replay import read_log_lines

    jobs, _, out = _catch_up(list(read_log_lines(USER_LOG)))
    assert jobs == []
    assert not any("本局 AI 调用" in line for line in out)


@NEEDS_USER_LOG
def test_watch_catch_up_mid_game_calls_once_for_the_current_turn():
    from helpers import TEST_POOL, USER_LOG

    from lscs.pipeline import Pipeline
    from lscs.replay import read_log_lines
    from lscs.state.models import EventType
    from lscs.state.tracker import StateTracker

    lines = list(read_log_lines(USER_LOG))
    started = []

    def on_update(_state, events):
        started.extend(e.turn for e in events if e.type == EventType.RECRUIT_STARTED)

    pipeline = Pipeline(StateTracker(TEST_POOL), on_update)
    cut = next(i for i, line in enumerate(lines) if pipeline.push(line) or 7 in started)
    jobs, provider, _ = _catch_up(lines[: cut + 200])  # 第 7 回合招募中途启动 watch
    assert len(jobs) == 2  # 快速、深度各一个，都只为当前回合
    for job in jobs:
        job()
    assert {c.thinking for c in provider.calls} == {"disabled", "low"}
    assert all(c.user.split("\n\n", 1)[1].startswith("第 7 回合") for c in provider.calls)


@NEEDS_USER_LOG
def test_watch_catch_up_during_hero_select_calls_the_hero_lane_once():
    from helpers import TEST_POOL, USER_LOG

    from lscs.pipeline import Pipeline
    from lscs.replay import read_log_lines
    from lscs.state.models import EventType
    from lscs.state.tracker import StateTracker

    lines = list(read_log_lines(USER_LOG))
    offered = []

    def on_update(_state, events):
        offered.extend(e for e in events if e.type == EventType.HEROES_OFFERED)

    pipeline = Pipeline(StateTracker(TEST_POOL), on_update)
    cut = next(i for i, line in enumerate(lines) if pipeline.push(line) or offered)
    jobs, provider, out = _catch_up(lines[: cut + 50])  # 选英雄时启动 watch（离选定还有 28 秒）
    assert len(jobs) == 1
    jobs[0]()
    assert provider.calls[0].user.startswith("选英雄（4 选 1）")
    assert out[0].startswith("  📋 规则：选 BG21_HERO_000（这几个英雄里它护甲最少")  # 凯瑞尔


def test_watch_stops_when_asked():
    from helpers import TEST_POOL

    from lscs.cli import watch
    from lscs.pipeline import Pipeline
    from lscs.state.tracker import StateTracker

    class FakeTailer:
        path = None
        polls = 0

        def poll(self):
            self.polls += 1
            return []

    tailer = FakeTailer()
    pipeline = Pipeline(StateTracker(TEST_POOL), lambda s, e: None)
    watch(tailer, pipeline, interval=0, sleep=lambda _: None, stop=lambda: tailer.polls >= 3)
    assert tailer.polls == 3


def test_overlay_flags():
    from lscs.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["watch", "--no-overlay"]).no_overlay is True
    assert parser.parse_args(["watch"]).no_overlay is False
    assert parser.parse_args(["replay", "x.log", "--overlay"]).overlay is True
    assert parser.parse_args(["replay", "x.log"]).overlay is False


def _watch_setup(monkeypatch, tmp_path, mini_lines, config_text=None):
    """不读真实的 config.toml；watch 换成假的：把 mini 录像推进 Pipeline，然后等停止标志。"""
    import threading

    from helpers import FIXTURES

    import lscs.cli
    from lscs.cards.db import CardDB

    config = tmp_path / "config.toml"
    if config_text is not None:
        config.write_text(config_text, encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", config)
    monkeypatch.setattr(
        lscs.cli, "_load_cards", lambda: CardDB.from_json_file(FIXTURES / "cards_mini.json")
    )
    calls = []

    def fake_watch(tailer, pipeline, interval, tick=None, stop=None, sleep=None):
        calls.append({"thread": threading.current_thread().name, "stop": stop})
        for line in mini_lines:
            pipeline.push(line)
        pipeline.flush()
        while stop is not None and not stop():
            sleep(0.01)

    monkeypatch.setattr(lscs.cli, "watch", fake_watch)
    install = tmp_path / "Hearthstone"
    install.mkdir()
    return ["watch", "--install-dir", str(install), "--no-ai"], calls


def test_watch_falls_back_to_terminal_when_overlay_cannot_start(
    monkeypatch, tmp_path, mini_lines, capsys
):
    import lscs.overlay.app

    def no_tk(_cfg, highlight=None):
        raise ImportError("No module named '_tkinter'")

    monkeypatch.setattr(lscs.overlay.app, "create_tk_window", no_tk)
    argv, calls = _watch_setup(monkeypatch, tmp_path, mini_lines)
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "悬浮面板无法启动（Python 没有安装 tkinter），只在终端显示建议。" in out
    assert "📋 规则：① 买 刷新畸体 1/4（商店里身材最好，有嘲讽）" in out  # 终端照常
    assert calls == [{"thread": "MainThread", "stop": None}]


def test_watch_no_overlay_flag_keeps_terminal_only(monkeypatch, tmp_path, mini_lines, capsys):
    import lscs.overlay.app

    def must_not_open(_cfg, highlight=None):
        raise AssertionError("不该打开面板")

    monkeypatch.setattr(lscs.overlay.app, "create_tk_window", must_not_open)
    argv, calls = _watch_setup(monkeypatch, tmp_path, mini_lines)
    assert main([*argv, "--no-overlay"]) == 0
    out = capsys.readouterr().out
    assert "悬浮面板" not in out
    assert "📋 规则" in out
    assert calls == [{"thread": "MainThread", "stop": None}]


def test_watch_overlay_disabled_in_config(monkeypatch, tmp_path, mini_lines, capsys):
    import lscs.overlay.app

    monkeypatch.setattr(lscs.overlay.app, "create_tk_window", lambda _cfg, highlight=None: 1 / 0)
    argv, calls = _watch_setup(monkeypatch, tmp_path, mini_lines, "[overlay]\nenabled = false\n")
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert "悬浮面板已在 config.toml 中关闭，只在终端显示建议。" in out
    assert calls[0]["stop"] is None


def test_watch_with_broken_overlay_config_fails(monkeypatch, tmp_path, mini_lines, capsys):
    argv, calls = _watch_setup(monkeypatch, tmp_path, mini_lines, '[overlay]\nposition = "中间"\n')
    assert main(argv) == 1
    assert "overlay.position" in capsys.readouterr().out
    assert calls == []


def test_watch_with_overlay_feeds_panel_and_stops_on_ctrl_c(
    monkeypatch, tmp_path, mini_lines, capsys
):
    import signal
    import time

    import lscs.overlay.app

    seen = []

    class FakeWindow:
        status = "悬浮面板已打开（测试）"

        def run(self, model, keep_running):
            deadline = time.monotonic() + 10
            while keep_running():
                seen.append(model.snapshot())
                if seen[-1].title.startswith("对局结束"):
                    signal.raise_signal(signal.SIGINT)
                assert time.monotonic() < deadline
                time.sleep(0.01)

    highlights = []

    def create(_cfg, highlight=None):
        highlights.append(highlight)
        return FakeWindow()

    monkeypatch.setattr(lscs.overlay.app, "create_tk_window", create)
    argv, calls = _watch_setup(monkeypatch, tmp_path, mini_lines, "[highlight]\nshop_y = 0.3\n")
    assert main(argv) == 0
    assert highlights[0].shop_y == 0.3  # config.toml 的 [highlight] 交给面板窗口
    out = capsys.readouterr().out
    assert "悬浮面板已打开（测试）" in out
    assert "📋 规则：① 买 刷新畸体 1/4（商店里身材最好，有嘲讽）" in out  # 终端照常
    assert calls[0]["thread"] == "lscs-watch" and calls[0]["stop"] is not None
    assert seen[-1].title == "对局结束 · 第 5 名"
    assert seen[-1].version > 1


def test_replay_with_overlay_previews_and_closes_when_done(monkeypatch, tmp_path, capsys):
    import time

    from helpers import FIXTURES, MINI_LOG

    import lscs.cli
    import lscs.overlay.app
    from lscs.cards.db import CardDB

    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", tmp_path / "config.toml")
    monkeypatch.setattr(
        lscs.cli, "_load_cards", lambda: CardDB.from_json_file(FIXTURES / "cards_mini.json")
    )
    titles = []

    class FakeWindow:
        status = "预览"

        def run(self, model, keep_running):
            deadline = time.monotonic() + 10
            while keep_running():
                titles.append(model.snapshot().title)
                if titles[-1] == "回放结束":
                    return
                assert time.monotonic() < deadline
                time.sleep(0.005)

    monkeypatch.setattr(
        lscs.overlay.app, "create_tk_window", lambda _cfg, highlight=None: FakeWindow()
    )
    assert main(["replay", str(MINI_LOG), "--overlay", "--speed", "1000"]) == 0
    out = capsys.readouterr().out
    assert "[T 1] 招募开始" in out  # --overlay 隐含 --advice，并且显示中文卡名
    assert "📋 规则：① 买 刷新畸体 1/4（商店里身材最好，有嘲讽）" in out
    assert titles[-1] == "回放结束"


def test_watch_stops_in_the_middle_of_a_big_first_poll():
    """watch 启动时从头读整个 Power.log（可能几十万行）；按 Ctrl+C 后不能等全部推完。"""
    import time

    from lscs.cli import watch

    class FakeTailer:
        path = None

        def poll(self):
            return ["D 10:00:00.0000000 line"] * 200_000

    class CountingPipeline:
        pushes = 0

        def push(self, _line):
            self.pushes += 1

        def flush(self):
            raise AssertionError("停止后不该再 flush")

    pipeline = CountingPipeline()
    started = time.monotonic()
    watch(FakeTailer(), pipeline, 0, sleep=lambda _: None, stop=lambda: pipeline.pushes >= 10)
    assert pipeline.pushes == 10
    assert time.monotonic() - started < 1


def test_overlay_check_command(monkeypatch, tmp_path, capsys):
    import lscs.cli
    import lscs.overlay.check

    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", tmp_path / "config.toml")
    seen = []

    def fake_check(cfg):
        seen.append(cfg)
        print("√ 假的检查")
        return True

    monkeypatch.setattr(lscs.overlay.check, "run_check", fake_check)
    assert main(["overlay-check"]) == 0
    assert "√ 假的检查" in capsys.readouterr().out
    assert seen[0].opacity == 0.85
    monkeypatch.setattr(lscs.overlay.check, "run_check", lambda cfg: False)
    assert main(["overlay-check"]) == 1


def test_overlay_check_on_this_platform_is_harmless(monkeypatch, tmp_path, capsys):
    import sys

    import lscs.cli

    if sys.platform == "win32":
        pytest.skip("Windows 上会真的创建窗口")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", tmp_path / "config.toml")
    assert main(["overlay-check"]) == 1
    assert "不是 Windows" in capsys.readouterr().out


def test_overlay_check_with_broken_config(monkeypatch, tmp_path, capsys):
    import lscs.cli

    config = tmp_path / "config.toml"
    config.write_text("[overlay]\nopacity = 2\n", encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", config)
    assert main(["overlay-check"]) == 1
    assert "overlay.opacity" in capsys.readouterr().out


def test_highlight_test_command(monkeypatch, tmp_path, capsys):
    import lscs.cli
    import lscs.overlay.calibrate

    config = tmp_path / "config.toml"
    config.write_text("[highlight]\nshop_y = 0.3\n", encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", config)
    calls = []

    def fake_run(cfg, seconds, rows):
        calls.append((cfg.shop_y, seconds, list(rows)))
        return True

    monkeypatch.setattr(lscs.overlay.calibrate, "run_highlight_test", fake_run)
    assert main(["highlight-test"]) == 0
    assert main(["highlight-test", "--seconds", "3", "--rows", "shop, buttons"]) == 0
    assert calls == [
        (0.3, 10.0, ["shop", "board", "choice", "choice4", "hero", "buttons"]),
        (0.3, 3.0, ["shop", "buttons"]),
    ]
    monkeypatch.setattr(lscs.overlay.calibrate, "run_highlight_test", lambda *_: False)
    assert main(["highlight-test"]) == 1


def test_highlight_test_rejects_bad_arguments_and_config(monkeypatch, tmp_path, capsys):
    import lscs.cli

    with pytest.raises(SystemExit):
        main(["highlight-test", "--rows", "shop,hand"])
    assert "不认识：hand" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["highlight-test", "--seconds", "0"])
    config = tmp_path / "config.toml"
    config.write_text("[highlight]\nshop_y = 9\n", encoding="utf-8")
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", config)
    assert main(["highlight-test"]) == 1
    assert "highlight.shop_y" in capsys.readouterr().out


def test_highlight_test_on_this_platform_previews(monkeypatch, tmp_path, capsys):
    """Mac 上走预览分支（窗口换成假的）；没有 config.toml 时用默认值。"""
    import lscs.cli
    import lscs.overlay.calibrate
    from lscs.overlay import win32

    monkeypatch.setattr(win32, "IS_WINDOWS", False)
    monkeypatch.setattr(lscs.cli, "DEFAULT_CONFIG", tmp_path / "config.toml")
    shown = []
    monkeypatch.setattr(
        lscs.overlay.calibrate, "show_tk", lambda frames, *rest: shown.append(len(frames))
    )
    assert main(["highlight-test", "--seconds", "1"]) == 0
    assert shown == [29]
    assert "不是 Windows：在一个 1280×720 的预览窗口里画出所有位置" in capsys.readouterr().out
