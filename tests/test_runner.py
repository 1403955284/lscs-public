from pathlib import Path

from factory import make_state, shop_minion
from helpers import TEST_POOL

from lscs.advisor.display import AdviceDisplay
from lscs.advisor.rules_config import load_rules_config
from lscs.advisor.runner import AdviceRunner
from lscs.pipeline import Pipeline
from lscs.state.models import Event, EventType
from lscs.state.tracker import StateTracker

RULES = load_rules_config(
    Path(__file__).resolve().parents[1] / "strategy" / "current" / "rules.toml"
)


def run_with_runner(lines, runner):
    pipeline = Pipeline(StateTracker(TEST_POOL), runner.on_update)
    for line in lines:
        pipeline.push(line)
    pipeline.flush()


def test_rules_advice_printed_during_recruit(mini_lines):
    out = []
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append))
    run_with_runner(mini_lines, runner)
    assert out == [
        "  📋 规则：护甲信息不全，等 AI 建议",  # 选英雄：这份合成日志的英雄没有护甲标签
        "  📋 规则：① 买 BGS_116 1/4（商店里身材最好，有嘲讽）",
        "          ② 刷新（剩 1 金，刷新找更好的牌）",  # 米尔豪斯买牌只要 2 金
        "  📋 规则：① 升本 → 2 本（第 2 回合按节奏该升 2 本了）   方向：海盗",
    ]
    assert runner.last_rules.turn == 2


def test_rules_error_does_not_stop(mini_lines, monkeypatch):
    import lscs.advisor.runner

    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(lscs.advisor.runner, "rule_plans", boom)
    out = []
    spy = SpyScheduler()
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append), spy)
    run_with_runner(mini_lines, runner)
    # 每回合只提示一次（第 1、2 回合）；选英雄的规则不受影响
    assert out == ["  📋 规则：护甲信息不全，等 AI 建议"] + ["  📋 本回合规则建议出错"] * 2
    # 规则出错时照样把局面交给 AI 调度器（招募阶段的更新都带 ctx）
    assert sum(has_ctx for _, has_ctx in spy.observed) >= 2


class SpyScheduler:
    calls = input_tokens = output_tokens = 0

    def __init__(self):
        self.observed = []
        self.phases = []  # (阶段, 有没有 ctx)

    def reset_game(self):
        pass

    def observe(self, state, events, ctx):
        self.observed.append((state.seq, ctx is not None))
        self.phases.append((state.phase.value, ctx is not None))

    def tick(self):
        return []


def _events(*types, turn=2):
    return [Event(type=t, turn=turn) for t in types]


def test_no_advice_before_main_action():
    out = []
    spy = SpyScheduler()
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append), spy)
    shop = [shop_minion(20, "BGS_061", 3, 1)]
    early = make_state(turn=2, gold=4, shop=(), main_action=False)  # 铸币先到，商店还是空的
    runner.on_update(early, _events(EventType.GOLD_CHANGED))
    filled = make_state(turn=2, gold=4, shop=shop, seq=2, main_action=False)
    runner.on_update(filled, _events(EventType.SHOP_CHANGED))
    assert out == [] and spy.observed == []
    started = make_state(turn=2, gold=4, shop=shop, seq=3)
    runner.on_update(started, _events(EventType.RECRUIT_STARTED))
    assert out[0].startswith("  📋 规则：")
    assert spy.observed == [(3, True)]


def test_reconnect_without_recruit_started_still_advises():
    # 重连时 STEP=MAIN_ACTION 以初始值出现，不会有招募开始事件
    out = []
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append))
    state = make_state(turn=4, gold=6, shop=[shop_minion(20, "BGS_061", 3, 1)])
    runner.on_update(state, _events(EventType.GOLD_CHANGED, EventType.SHOP_CHANGED, turn=4))
    assert out and out[0].startswith("  📋 规则：")


def test_hero_power_change_updates_rules_advice():
    # 招募开始时选项还没到（技能状态未知）；选项到了、技能能用，规则建议跟着更新
    from factory import power

    out = []
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append))
    shop = [shop_minion(20, "BGS_061", 3, 1)]
    unknown = make_state(turn=2, gold=0, shop=shop, hero_power_state=power(option="unknown"))
    runner.on_update(unknown, _events(EventType.RECRUIT_STARTED))
    usable = make_state(turn=2, gold=0, shop=shop, seq=2, hero_power_state=power())
    runner.on_update(usable, _events(EventType.HERO_POWER_CHANGED))
    assert out == [
        "  📋 规则：无需操作（本回合不需要操作）",
        "  📋 规则：① 使用英雄技能 BG21_HERO_000p（免费技能，每回合都用掉）",
    ]


def hero_log_lines():
    from helpers import HERO_LOG

    from lscs.replay import read_log_lines

    return list(read_log_lines(HERO_LOG))


def test_hero_rules_shown_for_each_offer_only():
    out = []
    spy = SpyScheduler()
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append), spy)
    run_with_runner(hero_log_lines(), runner)
    reason = "这几个英雄里它护甲最少：官方给越强的英雄发越少的护甲"
    # 开局一次、重随一次；其他玩家的英雄出现、选完后的等待都不再提示
    assert [line for line in out if "选 " in line or "护甲" in line] == [
        f"  📋 规则：选 BG21_HERO_000（{reason}）"
    ] * 2
    assert runner.last_hero.pick == 11
    assert (15, "BG22_HERO_003") in runner.last_hero.signature  # 重随后的那一组
    # 选英雄阶段的每次更新都交给调度器（没有 ctx）
    hero_updates = [has_ctx for phase, has_ctx in spy.phases if phase == "HERO_SELECT"]
    assert hero_updates and not any(hero_updates)


def test_hero_rules_error_does_not_stop(monkeypatch):
    import lscs.advisor.runner

    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(lscs.advisor.runner, "rule_hero_pick", boom)
    out = []
    spy = SpyScheduler()
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append), spy)
    run_with_runner(hero_log_lines(), runner)
    assert out[0] == "  📋 选英雄规则建议出错"
    assert out.count("  📋 选英雄规则建议出错") == 1  # 重随时不再重复提示
    assert "  📋 本回合规则建议出错" not in out
    assert any(phase == "HERO_SELECT" for phase, _ in spy.phases)  # AI 照常调用
