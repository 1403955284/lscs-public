import json
from pathlib import Path

import pytest
from factory import make_state, minion, shop_minion
from helpers import FIXTURES, NEEDS_USER_LOG, REAL_LOG, TEST_POOL, USER_LOG

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


def make_runner(answers, out, deep=False):
    scheduler = LLMScheduler(
        FakeProvider(answers),
        LLMConfig(api_key="sk-x-123456789", deep_analysis=deep),
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
        "          思路：先保血",
        "          方向：纳迦",
        "本局 AI 调用 2 次，输入 0.2 千、输出 0.0 千 token",
    ]


def test_fast_and_deep_advice_in_terminal(mini_lines):
    agree_turn_1 = json.dumps(
        {
            "analysis": "第一回合，买嘲讽。",
            "actions": [{"type": "buy", "entity_id": 32, "reason": "嘲讽"}, {"type": "refresh"}],
        }
    )
    deep_turn_1 = json.dumps(
        {
            "analysis": "前期先拿嘲讽保血，\n之后找元素。",
            "actions": [{"type": "buy", "entity_id": 32, "reason": "嘲讽"}],
            "direction": "元素",
            "look_for": ["刷新畸体", "熔岩潜伏者"],
        }
    )
    turn_2 = json.dumps(
        {
            "analysis": "血量健康，升本。",
            "actions": [{"type": "buy", "entity_id": 50, "reason": "身材好"}],
            "direction": "纳迦",
        }
    )
    deep_turn_2 = json.dumps(
        {
            "analysis": "纳迦核心在高本。",
            "actions": [{"type": "buy", "entity_id": 999, "reason": "不存在"}],
            "look_for": ["熔岩潜伏者"],
        }
    )
    out = []
    runner = make_runner([agree_turn_1, deep_turn_1, turn_2, deep_turn_2], out, deep=True)
    run(mini_lines, runner)
    assert out == [
        "  📋 规则：① 买 刷新畸体 1/4（商店里身材最好，有嘲讽）",
        "          ② 刷新（剩 1 金，刷新找更好的牌）",
        "  🤖 AI 分析中…",
        "  🤖 ✓ AI 同意规则建议（0.0 秒）",
        "          分析：第一回合，买嘲讽。",
        "  🧠 深度分析中…（约 20 秒）",
        "  🧠 深度分析（0 秒）：① 买 刷新畸体 1/4（嘲讽）",
        "          分析：前期先拿嘲讽保血， 之后找元素。",
        "          方向：元素   要找：刷新畸体、熔岩潜伏者",
        "  📋 规则：① 升本 → 2 本（第 2 回合按节奏该升 2 本了）   方向：海盗",
        "  🤖 AI 分析中…",
        "  🤖 AI（0.0 秒）：① 买 熔岩潜伏者 2/5（身材好）",
        "          分析：血量健康，升本。",
        "          方向：纳迦",
        "  🧠 深度分析中…（约 20 秒）",
        "  🧠 深度分析：操作不可用，只看思路",
        "          分析：纳迦核心在高本。",
        "          要找：熔岩潜伏者",
        "本局 AI 调用 4 次，输入 0.4 千、输出 0.1 千 token",
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


def result_for(old, plan=PLAN, started=100.0, lane="fast", expired=None):
    request = AIRequest(1, old, None, started, lane)
    return AIResult(request, plan, None, elapsed=3.0, expired=expired)


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
    runner._show_result(AIResult(AIRequest(1, make_state(), None, 0.0), None, "AI 超时", 20.0))
    assert out == ["  🤖 AI 超时"]


def test_deep_errors_do_not_look_like_fast_ones():
    runner, out = runner_with_state(make_state())
    deep = AIRequest(2, make_state(), None, 0.0, "deep")
    runner._show_result(AIResult(deep, None, "AI 超时", 77.5))
    runner._show_result(AIResult(deep, None, "DeepSeek 余额不足", 3.0))
    assert out == ["  🧠 深度分析超时", "  🧠 深度分析失败：DeepSeek 余额不足"]


THOUGHTS = PLAN.model_copy(
    update={"analysis": "血量健康，找对子。", "look_for": ("海盗无赖",), "direction": "海盗"}
)


def test_deep_result_after_refresh_shows_only_the_thoughts():
    old = make_state(gold=6, shop=SHOP)
    runner, out = runner_with_state(old.model_copy(update={"seq": 2}))
    runner._show_result(result_for(old, THOUGHTS, lane="deep", expired="refreshed"))
    assert out == [
        "  🧠 深度分析：操作已过期（商店刷新了），只看思路",
        "          分析：血量健康，找对子。",
        "          方向：海盗   要找：海盗无赖",
    ]


def test_deep_result_on_a_changed_board_keeps_the_thoughts():
    old = make_state(gold=6, shop=SHOP)
    poorer = make_state(gold=1, shop=SHOP, seq=3)  # 钱花在别处了，剩下的步骤买不起
    runner, out = runner_with_state(poorer)
    runner._show_result(result_for(old, THOUGHTS, lane="deep"))
    assert out[0] == "  🧠 深度分析：操作已过期，只看思路"
    done = make_state(
        gold=0, shop=(), board=[minion(20, "BGS_061", 3, 1), minion(21, "BG21_029", 1, 1)], seq=4
    )
    runner._state = done  # 深度分析回来时步骤都做完了：仍然看思路
    runner._show_result(result_for(old, THOUGHTS, lane="deep"))
    assert out[3] == "  🧠 深度分析：操作已过期，只看思路"


def test_deep_result_in_time_shows_steps_with_whole_seconds():
    old = make_state(gold=6, shop=SHOP)
    runner, out = runner_with_state(old)
    result = AIResult(AIRequest(1, old, None, 100.0, "deep"), THOUGHTS, None, elapsed=23.4)
    runner._show_result(result)
    assert out == [
        "  🧠 深度分析（23 秒）：① 买 海盗无赖 3/1（好）",
        "          ② 买 粗鄙小鬼 1/1（也好）",
        "          分析：血量健康，找对子。",
        "          方向：海盗   要找：海盗无赖",
    ]


class BrokenScheduler:
    def tick(self):
        raise RuntimeError("坏了")


def test_tick_errors_do_not_stop_watch():
    out = []
    display = AdviceDisplay(CARDS.name, out.append)
    runner = AdviceRunner(RULES, CARDS.name, display, BrokenScheduler())
    runner.tick()
    assert out == ["  🤖 AI 结果处理出错，本次忽略"]


def test_remaining_plan_drops_hero_power_once_used():
    from factory import power

    plan = Plan(source="llm", actions=(Action(type="hero_power"), Action(type="refresh")))
    old = make_state(shop=SHOP, hero_power_state=power())
    still = make_state(shop=SHOP, hero_power_state=power(), seq=2)
    used = make_state(shop=SHOP, hero_power_state=power(used=True), seq=3)
    assert remaining_plan(plan, old, still).actions == plan.actions
    assert remaining_plan(plan, old, used).actions == (Action(type="refresh"),)
    by_options = make_state(
        shop=SHOP,
        hero_power_state=power(option="unusable", reason="REQ_NOT_EXHAUSTED_HERO_POWER"),
        seq=4,
    )
    assert remaining_plan(plan, old, by_options).actions == (Action(type="refresh"),)


def test_ai_plan_with_a_needless_target_agrees_with_rules():
    from factory import power

    from lscs.state.models import Event, EventType

    answer = json.dumps({"actions": [{"type": "hero_power", "entity_id": 99, "reason": "免费"}]})
    out = []
    runner = make_runner([answer], out)
    state = make_state(turn=3, gold=0, shop=SHOP, hero_power_state=power())
    runner.on_update(state, [Event(type=EventType.RECRUIT_STARTED, turn=3)])
    runner.tick()
    assert out[-1] == "  🤖 ✓ AI 同意规则建议（0.0 秒）"


@pytest.mark.parametrize(
    ("path", "line"),
    [
        (REAL_LOG, "英雄技能：法力风暴（被动，不能主动使用——不要安排 hero_power）"),
        pytest.param(
            USER_LOG,
            "英雄技能：BG21_HERO_000p（0 金，本回合可用，不需要目标）",
            marks=NEEDS_USER_LOG,
        ),
    ],
)
def test_ai_request_at_recruit_start_already_knows_the_hero_power(path, line):
    from lscs.replay import read_log_lines

    provider = FakeProvider([])
    scheduler = LLMScheduler(
        provider,
        LLMConfig(api_key="sk-x-123456789", max_calls_per_game=40),
        CARDS,
        guide="",
        clock=lambda: 100.0,
        run_async=lambda job: job(),
    )
    runner = AdviceRunner(RULES, CARDS.name, AdviceDisplay(CARDS.name, lambda _: None), scheduler)
    run(read_log_lines(path), runner)
    situations = [c.user.split("\n\n", 1)[1] for c in provider.calls]  # 去掉开头的本局卡池
    starts = [text for text in situations if text.startswith("第 1 回合")]
    assert starts and any(text.startswith(line) for text in starts[0].splitlines())


def test_deep_result_arriving_in_combat_still_shows_the_thoughts():
    old = make_state(gold=6, shop=SHOP, turn=3)
    runner, out = runner_with_state(make_state(turn=3, phase=Phase.COMBAT, seq=2))
    runner._show_result(result_for(old, THOUGHTS, lane="deep"))
    assert out == [
        "  🧠 深度分析：操作已过期，只看思路",
        "          分析：血量健康，找对子。",
        "          方向：海盗   要找：海盗无赖",
    ]
    runner._show_result(result_for(old, THOUGHTS))  # 快速建议在战斗中回来：照旧丢弃
    over = make_state(turn=3, phase=Phase.GAME_OVER, seq=3)
    runner._state = over  # 对局已经结束：不再显示
    runner._show_result(result_for(old, THOUGHTS, lane="deep"))
    runner._state = make_state(turn=4, phase=Phase.COMBAT, seq=4)  # 别的回合：丢弃
    runner._show_result(result_for(old, THOUGHTS, lane="deep"))
    assert len(out) == 3
