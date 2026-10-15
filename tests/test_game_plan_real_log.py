"""用户真实对局回放整局计划（整局计划与流派指南设计文档第 10 节）：假的 AI 在第 5 回合的深度分析
里定下计划、之后照抄、第 9 回合改成"保血"。不联网，不读 config.toml。"""

import json
import re
from pathlib import Path

import pytest
from helpers import FIXTURES, NEEDS_USER_LOG, USER_LOG

from lscs.advisor.display import AdviceDisplay
from lscs.advisor.rules_config import load_rules_config
from lscs.advisor.runner import AdviceRunner
from lscs.cards.db import CardDB
from lscs.cli import DEFAULT_POOL
from lscs.config import LLMConfig
from lscs.llm.provider import FakeCall, FakeProvider, LLMResult
from lscs.llm.scheduler import LLMScheduler
from lscs.pipeline import Pipeline
from lscs.replay import read_log_lines
from lscs.state.pool import load_pool_config
from lscs.state.tracker import StateTracker

pytestmark = NEEDS_USER_LOG

ROOT = Path(__file__).resolve().parents[1]
RULES = load_rules_config(ROOT / "strategy" / "current" / "rules.toml")
CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")
TURN = re.compile(r"^第 (\d+) 回合（招募）", re.M)
PLAN_5 = {
    "comp": "畸变神明",
    "tribe": "畸变",
    "key_cards": ["蚀脑怪", "飘荡祭品"],
    "tiers": "第 6 回合 4 本，第 8 回合 5 本",
    "roll": "5 本后刷新找核心卡",
    "stage": "过渡",
    "pivot": "第 8 回合还没有两张核心卡就转海盗",
}
# 第 7、8 回合：实测里 AI 换个说法照抄（流派名带括号、节奏去掉空格），不算改计划
PLAN_7 = dict(PLAN_5, comp="畸变神明（畸变）", tiers="第6回合4本，第8回合5本")
# 第 9 回合真的改了：换了一张核心卡、改成保血
PLAN_9 = dict(
    PLAN_5,
    key_cards=["蚀脑怪", "虚悯者"],
    stage="保血",
    roll="血量低于 15 先补身材",
    changed="连输两场",
)


class ScriptedProvider(FakeProvider):
    """按局面回答：选英雄、选项选第一个；招募的快速建议什么都不做；深度分析第 5 回合起带计划，
    第 7、8 回合换个说法照抄，第 9 回合起改成保血、换一张核心卡。"""

    def __init__(self):
        super().__init__([])

    def complete_json(self, system, user, timeout, thinking=None, max_tokens=None, turns=()):
        self.calls.append(FakeCall(system, user, timeout, thinking, max_tokens, tuple(turns)))
        return LLMResult(self._answer(user, thinking), 1.0, input_tokens=100, output_tokens=20)

    @staticmethod
    def _answer(user, thinking):
        if user.startswith("选英雄"):
            return json.dumps({"pick": int(re.search(r"\[#(\d+)\]", user).group(1))})
        if user.startswith("选择："):
            return json.dumps({"pick": int(re.search(r"\[#(\d+)\] 左 1 ", user).group(1))})
        turn = int(TURN.search(user).group(1))
        answer = {"analysis": "按计划走", "actions": []}
        if thinking == "low" and turn >= 5:
            answer["plan"] = PLAN_9 if turn >= 9 else PLAN_7 if turn >= 7 else PLAN_5
        return json.dumps(answer, ensure_ascii=False)


def _turn(user):
    return int(TURN.search(user).group(1))


@pytest.fixture(scope="module")
def replay():
    """把用户录像推两遍（第二遍是新的一局）：返回 (终端输出, 第一局的调用, 第二局的调用)。"""
    provider = ScriptedProvider()
    scheduler = LLMScheduler(
        provider,
        LLMConfig(api_key="sk-x-123456789", max_calls_per_game=300),
        CARDS,
        guide="要点",
        debounce=0.0,
        hero_debounce=0.0,
        clock=lambda: 100.0,
        run_async=lambda job: job(),
    )
    out = []
    runner = AdviceRunner(RULES, CARDS.name, AdviceDisplay(CARDS.name, out.append), scheduler)

    def on_update(state, events):
        runner.on_update(state, events)
        runner.tick()

    lines = list(read_log_lines(USER_LOG))
    pipeline = Pipeline(StateTracker(load_pool_config(DEFAULT_POOL)), on_update)
    for line in lines:
        pipeline.push(line)
    pipeline.flush()
    first_game = list(provider.calls)
    first_out = list(out)
    for line in lines:  # 再打一局（同一份录像）
        pipeline.push(line)
    pipeline.flush()
    return first_out, first_game, provider.calls[len(first_game) :]


def recruit_calls(calls, lane=None):
    """招募的调用（不是改正）；lane：fast 不开思考，deep 开思考。"""
    thinking = {"fast": "disabled", "deep": "low", None: None}[lane]
    return [
        c
        for c in calls
        if c.user.startswith("【本局卡池") and (thinking is None or c.thinking == thinking)
    ]


def test_fast_advice_follows_the_plan_set_on_turn_5(replay):
    _, calls, _ = replay
    fast = recruit_calls(calls, "fast")
    by_turn: dict[int, list[str]] = {}
    for call in fast:
        by_turn.setdefault(_turn(call.user), []).append(call.user)
    assert sorted(by_turn) == list(range(1, 14))
    for turn in range(1, 5):
        assert all("【整局计划】还没定" in user for user in by_turn[turn]), turn
    for turn in range(6, 9):
        assert all("【整局计划（第 5 回合定" in user for user in by_turn[turn]), turn
        assert all("| 阶段 过渡 |" in user for user in by_turn[turn]), turn
    # 第 7 回合深度分析换了个说法：不算改计划（还是"第 5 回合定"），只悄悄换成新的措辞
    assert all("| 节奏 第6回合4本，第8回合5本 |" in user for user in by_turn[8])
    # 第 9 回合只有回合开始那一次快速建议，和深度分析同时发出，还是第 5 回合的计划
    assert all("【整局计划（第 5 回合定" in user for user in by_turn[9])
    for turn in range(10, 14):
        assert all("【整局计划（第 9 回合定" in user for user in by_turn[turn]), turn
        assert all("| 阶段 保血 |" in user for user in by_turn[turn]), turn


def test_the_recruit_system_prompt_stays_the_same(replay):
    _, calls, _ = replay
    systems = {c.system for c in recruit_calls(calls)}
    assert len(systems) == 1 and "【整局计划】\n- plan 是整局的计划" in systems.pop()


def test_terminal_prints_the_plan_once_when_set_and_once_when_changed(replay):
    out, _, _ = replay
    assert sum(line.startswith("  🗺 计划：畸变神明 · ") for line in out) == 1
    assert sum(line.startswith("  🗺 计划改了：畸变神明 · ") for line in out) == 1
    assert sum("🗺" in line for line in out) == 2
    changed = next(i for i, line in enumerate(out) if line.startswith("  🗺 计划改了："))
    assert out[changed + 1 : changed + 3] == [
        "          原因：连输两场",
        "          规则：血量低于 15 先补身材",
    ]


def test_choice_messages_carry_the_plan_after_it_is_set(replay):
    _, calls, _ = replay
    choices = [c.user for c in calls if c.user.startswith("选择：")]
    after = [user for user in choices if _turn(user) >= 6]
    titles = [user.splitlines()[0] for user in after]
    assert any(title.startswith("选择：黑暗之赐") for title in titles)
    assert any("Triples" in title for title in titles)  # 三连奖励（迷你卡牌数据里没有它的名字）
    assert all("\n整局计划：目标 畸变神明 · 核心卡 " in user for user in after)
    assert not any("整局计划" in user for user in choices if _turn(user) < 5)


def test_a_key_card_already_on_the_board_is_not_a_deviation(replay):
    # 第 6 回合商店里有蚀脑怪，但场上已经有一张（核心卡打 ✓）：不提醒，也不算偏离计划。
    # 之后的回合商店里的核心卡要么已经有，要么场面满了（偏离次数的写法见 test_runner_ai）
    out, calls, _ = replay
    turn_6 = [c.user for c in recruit_calls(calls, "fast") if _turn(c.user) == 6]
    assert any(
        "核心卡 蚀脑怪✓" in user and "] 蚀脑怪 " in user.split("商店：")[1] for user in turn_6
    )
    reminders = [line.strip() for line in out if "⚠ 计划提醒" in line]
    # 只有第 5 回合定下计划的那次深度分析：商店里有两张飘荡祭品、它自己没买（深度不计数）
    assert reminders == ["⚠ 计划提醒：商店里有核心卡 飘荡祭品，方案却没买它"]
    summary = next(line for line in out if line.startswith("本局 AI 调用"))
    assert summary.endswith(" token")  # 没有偏离：不写后半句


def test_a_new_game_starts_without_a_plan(replay):
    _, _, second = replay
    first = recruit_calls(second)[0]
    assert _turn(first.user) == 1 and "【整局计划】还没定" in first.user
