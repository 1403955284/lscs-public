"""用两份真实对局验证规则建议：每个招募回合都有合法建议，且从不出错。"""

from pathlib import Path

import pytest
from helpers import NEEDS_USER_LOG, REAL_LOG, USER_LOG

from lscs.advisor.context import build_context
from lscs.advisor.display import AdviceDisplay
from lscs.advisor.rules_config import load_rules_config
from lscs.advisor.runner import AdviceRunner
from lscs.advisor.validate import validate_plan
from lscs.cli import DEFAULT_POOL
from lscs.dump import describe
from lscs.pipeline import Pipeline
from lscs.replay import read_log_lines
from lscs.state.models import EventType, Phase
from lscs.state.pool import load_pool_config
from lscs.state.tracker import StateTracker

RULES = load_rules_config(
    Path(__file__).resolve().parents[1] / "strategy" / "current" / "rules.toml"
)


def replay_with_rules(path):
    """返回 (终端输出：时间线和建议按打印顺序交错, 每个招募开始时的规则方案)。"""
    out, checked = [], []
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append))

    def on_update(state, events):
        out.extend(describe(state, events))
        runner.on_update(state, events)
        if any(e.type == EventType.RECRUIT_STARTED for e in events):
            advice = runner.last_rules
            assert advice is not None and advice.state_seq == state.seq
            ctx = build_context(state, RULES.buy_cost_for(state.me.hero_card_id))
            assert validate_plan(advice.plan.actions, ctx) is None
            checked.append((state.turn, advice.plan))

    pipeline = Pipeline(StateTracker(load_pool_config(DEFAULT_POOL)), on_update)
    for line in read_log_lines(path):
        pipeline.push(line)
    pipeline.flush()
    return out, checked


@pytest.mark.parametrize(
    ("path", "recruits"), [(REAL_LOG, 16), pytest.param(USER_LOG, 13, marks=NEEDS_USER_LOG)]
)
def test_every_recruit_turn_gets_valid_rules_advice(path, recruits):
    out, checked = replay_with_rules(path)
    assert len(checked) == recruits
    assert not any("出错" in line for line in out)


@NEEDS_USER_LOG
def test_user_game_turn_2_suggests_upgrading():
    _, checked = replay_with_rules(USER_LOG)
    turn, plan = checked[1]
    assert turn == 2
    assert [a.type for a in plan.actions] == ["upgrade", "hero_power"]  # 定罪 0 费，每回合都用


@pytest.mark.parametrize("path", [REAL_LOG, pytest.param(USER_LOG, marks=NEEDS_USER_LOG)])
def test_no_advice_between_combat_and_next_recruit_start(path):
    # 回合开始时先发铸币（商店还是空的）、再摆商店，还可能先选饰品花掉铸币；
    # 这些都在"招募开始"之前，不能据此给建议
    out, _ = replay_with_rules(path)
    waiting = False
    for line in out:
        if "战斗开始" in line or "战斗结束" in line:
            waiting = True
        elif "招募开始" in line:
            waiting = False
        elif "📋" in line:
            assert not waiting, line


@pytest.mark.parametrize("path", [REAL_LOG, pytest.param(USER_LOG, marks=NEEDS_USER_LOG)])
def test_each_recruit_header_is_followed_by_its_own_advice(path):
    out, _ = replay_with_rules(path)
    headers = [i for i, line in enumerate(out) if "招募开始" in line]
    assert headers
    for i in headers:
        # 标题后面是商店、场面、手牌三行，然后是对手一览（活着的对手、残影各一行，
        # 其他淘汰的合成一行），紧接着就是这个回合的规则建议
        assert out[i + 4].startswith("  👥 对手"), out[i : i + 5]
        end = next(j for j in range(i + 5, len(out)) if out[j].startswith("  📋"))
        rows = out[i + 5 : end]
        assert rows and all(" 名 " in r or r.strip().startswith("已淘汰：") for r in rows), rows
        assert out[end].startswith("  📋 规则："), out[i : end + 1]


@pytest.mark.parametrize("path", [REAL_LOG, pytest.param(USER_LOG, marks=NEEDS_USER_LOG)])
def test_main_action_starts_with_recruit_started(path):
    started: set[int] = set()
    pipeline_updates = []
    pipeline = Pipeline(
        StateTracker(load_pool_config(DEFAULT_POOL)),
        lambda s, e: pipeline_updates.append((s, e)),
    )
    for line in read_log_lines(path):
        pipeline.push(line)
    pipeline.flush()
    recruit_updates = 0
    for state, events in pipeline_updates:
        if state.phase != Phase.RECRUIT:
            continue
        recruit_updates += 1
        if any(e.type == EventType.RECRUIT_STARTED for e in events):
            assert state.main_action
            started.add(state.turn)
        else:
            assert state.main_action == (state.turn in started), (state.turn, state.seq)
    assert recruit_updates > len(started) > 0
