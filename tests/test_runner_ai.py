import json
from pathlib import Path

from factory import make_state, minion, shop_minion
from helpers import FIXTURES, TEST_POOL

from lscs.advisor.display import AdviceDisplay
from lscs.advisor.models import Action, Plan
from lscs.advisor.rules_config import load_rules_config
from lscs.advisor.runner import AdviceRunner, remaining_plan
from lscs.cards.db import CardDB
from lscs.config import LLMConfig
from lscs.llm.provider import FakeProvider
from lscs.llm.scheduler import AIRequest, AIResult, LLMScheduler
from lscs.pipeline import Pipeline
from lscs.state.models import Phase
from lscs.state.tracker import StateTracker

RULES = load_rules_config(
    Path(__file__).resolve().parents[1] / "strategy" / "current" / "rules.toml"
)
CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")


def make_runner(answers, out):
    scheduler = LLMScheduler(
        FakeProvider(answers),
        LLMConfig(api_key="sk-x-123456789"),
        CARDS,
        guide="",
        debounce=0.0,
        clock=lambda: 100.0,
        run_async=lambda job: job(),
    )
    display = AdviceDisplay(CARDS.name, out.append)
    return AdviceRunner(RULES, CARDS.name, display, scheduler)


def run(lines, runner):
    def on_update(state, events):
        runner.on_update(state, events)
        runner.tick()

    pipeline = Pipeline(StateTracker(TEST_POOL), on_update)
    for line in lines:
        pipeline.push(line)
    pipeline.flush()


def test_ai_advice_follows_rules_in_terminal(mini_lines):
    agree_turn_1 = json.dumps(
        {"actions": [{"type": "buy", "entity_id": 32, "reason": "嘲讽"}, {"type": "refresh"}]}
    )
    turn_2 = json.dumps(
        {
            "actions": [{"type": "buy", "entity_id": 50, "reason": "身材好"}],
            "summary": "先保血",
            "direction": "纳迦",
        }
    )
    out = []
    runner = make_runner([agree_turn_1, turn_2], out)
    run(mini_lines, runner)
    assert out == [
        "  📋 规则：① 买 刷新畸体 1/4（商店里身材最好，有嘲讽）",
        "          ② 刷新（剩 1 金，刷新找更好的牌）",
        "  🤖 AI 分析中…",
        "  🤖 ✓ AI 同意规则建议（0.0 秒）",
        "  📋 规则：① 升本 → 2 本（第 2 回合按节奏该升 2 本了）   方向：海盗",
        "  🤖 AI 分析中…",
        "  🤖 AI（0.0 秒）：① 买 熔岩潜伏者 2/5（身材好）",
        "          思路：先保血   方向：纳迦",
        "本局 AI 调用 2 次，输入 0.2 千、输出 0.0 千 token",
    ]


SHOP = [shop_minion(20, "BGS_061", 3, 1), shop_minion(21, "BG21_029", 1, 1)]
PLAN = Plan(
    source="llm",
    actions=(
        Action(type="buy", entity_id=20, reason="好"),
        Action(type="buy", entity_id=21, reason="也好"),
    ),
)


def test_remaining_plan_drops_executed_steps():
    old = make_state(gold=6, shop=SHOP)
    new = make_state(gold=3, shop=SHOP[1:], board=[minion(20, "BGS_061", 3, 1)], seq=2)
    assert remaining_plan(PLAN, old, new).actions == PLAN.actions[1:]
    upgraded = make_state(tier=2, seq=2)
    plan = Plan(source="llm", actions=(Action(type="upgrade"), Action(type="refresh")))
    assert remaining_plan(plan, make_state(tier=1, shop=SHOP), upgraded).actions == (
        Action(type="refresh"),
    )


def result_for(old, plan=PLAN, started=100.0):
    request = AIRequest(1, old, None, (), started)
    return AIResult(request, plan, None, elapsed=3.0)


def runner_with_state(state, clock=lambda: 106.0):
    out = []
    runner = AdviceRunner(RULES, CARDS.name, AdviceDisplay(CARDS.name, out.append), clock=clock)
    runner._state = state
    return runner, out


def test_stale_result_is_trimmed_and_marked():
    old = make_state(gold=6, shop=SHOP)
    new = make_state(gold=3, shop=SHOP[1:], board=[minion(20, "BGS_061", 3, 1)], seq=2)
    runner, out = runner_with_state(new)
    runner._show_result(result_for(old))
    assert out == [
        "  🤖 AI（3.0 秒）：① 买 粗鄙小鬼 1/1（也好）",
        "          （基于 6 秒前的局面）",
    ]


def test_result_for_a_finished_turn_is_dropped():
    old = make_state(gold=6, shop=SHOP, turn=3)
    runner, out = runner_with_state(make_state(turn=3, phase=Phase.COMBAT, seq=2))
    runner._show_result(result_for(old))
    runner._state = make_state(turn=4, gold=6, shop=SHOP, seq=3)
    runner._show_result(result_for(old))
    assert out == []


def test_fully_executed_result_shows_nothing_and_broken_one_is_expired():
    old = make_state(gold=6, shop=SHOP)
    done = make_state(
        gold=0, shop=(), board=[minion(20, "BGS_061", 3, 1), minion(21, "BG21_029", 1, 1)], seq=2
    )
    runner, out = runner_with_state(done)
    runner._show_result(result_for(old))
    assert out == []
    poorer = make_state(gold=1, shop=SHOP, seq=3)  # 钱花在别处了，剩下的步骤买不起
    runner._state = poorer
    runner._show_result(result_for(old))
    assert out == ["  🤖 AI 建议已过期（局面变化太大）"]


def test_errors_are_shown_as_text():
    runner, out = runner_with_state(make_state())
    runner._show_result(AIResult(AIRequest(1, make_state(), None, (), 0.0), None, "AI 超时", 20.0))
    assert out == ["  🤖 AI 超时"]


class BrokenScheduler:
    def tick(self):
        raise RuntimeError("坏了")


def test_tick_errors_do_not_stop_watch():
    out = []
    display = AdviceDisplay(CARDS.name, out.append)
    runner = AdviceRunner(RULES, CARDS.name, display, BrokenScheduler())
    runner.tick()
    assert out == ["  🤖 AI 结果处理出错，本次忽略"]
