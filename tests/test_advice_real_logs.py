"""用两份真实对局验证规则建议：每个招募回合都有合法建议，且从不出错。"""

from pathlib import Path

import pytest
from helpers import NEEDS_STATS_FIXTURES, NEEDS_USER_LOG, REAL_LOG, USER_LOG

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


# ---- 本版本统计（本版本统计设计文档第 3.5、12 节）：用夹具里的统计回放两份真实日志 ----


class StatsTap:
    """只记下本版本统计的条目和选英雄的规则建议。"""

    def __init__(self):
        self.notes, self.heroes = [], []

    def show_stats(self, state, note):
        self.notes.append(note)

    def show_hero(self, state, advice, agrees=False):
        self.heroes.append(advice)

    def __getattr__(self, method):
        return lambda *args: None


def replay_with_stats(path):
    from helpers import fixture_stats

    stats = fixture_stats()
    tap = StatsTap()
    runner = AdviceRunner(RULES, lambda c: c, tap, stats=lambda: stats)
    pipeline = Pipeline(StateTracker(load_pool_config(DEFAULT_POOL)), runner.on_update)
    for line in read_log_lines(path):
        pipeline.push(line)
    pipeline.flush()
    return tap


@NEEDS_USER_LOG
@NEEDS_STATS_FIXTURES
def test_user_game_hero_and_trinket_stats():
    tap = replay_with_stats(USER_LOG)
    hero, early, late = tap.notes  # 选英雄一次，两次饰品；技能升级、黑暗之赐、三连不出条目
    assert (hero.kind, hero.best) == ("hero", 89)
    # 排名的分母是夹具里样本够的英雄数（10）
    assert [(r.entity_id, r.text) for r in hero.rows] == [
        (92, "第 4.04 名 · 选 27% · 第 7/10"),  # 了不起的杰弗里斯
        (89, "第 3.67 名 · 选 28% · 第 2/10"),  # 凯瑞尔·罗姆
        (90, "第 3.99 名 · 选 15% · 第 5/10"),  # 泰瑟兰·血望者
        (91, "第 3.91 名 · 选 29% · 第 4/10"),  # 洛，在世传奇
    ]
    assert [(a.pick, a.basis) for a in tap.heroes] == [(89, "stats")]  # 和用户实际选的一样
    # 第 6 回合：用户选了战绩最好的精神鞭笞者肖像
    assert (early.kind, early.choice_id, early.best) == ("trinket", 6, 3026)
    assert [(r.entity_id, r.text) for r in early.rows] == [
        (3027, "第 4.06 名 · 选 15%"),  # 沉没的船锚
        (3026, "第 3.89 名 · 选 27%"),  # 精神鞭笞者肖像
        (3025, "第 3.95 名 · 选 30%"),  # 神秘魔方
        (3028, "第 3.95 名 · 选 18%"),  # 麦迪文之书
    ]
    # 第 9 回合：邪恶试验；多彩罗盘是带 t 的那条（36068 局）
    assert (late.choice_id, late.best) == (10, 6629)
    assert [r.text for r in late.rows] == [
        "第 3.64 名 · 选 27%",
        "第 3.83 名 · 选 34%",
        "第 3.82 名 · 选 15%",
        "第 3.75 名 · 选 19%",
    ]


@NEEDS_STATS_FIXTURES
def test_cc0_game_finds_the_skin_hero_and_recommends_vanndar():
    tap = replay_with_stats(REAL_LOG)
    (hero,) = tap.notes
    assert [r.entity_id for r in hero.rows] == [92, 91, 93, 94]
    assert hero.rows[2].text == "第 4.08 名 · 选 10% · 第 9/10"  # 米尔豪斯（_SKIN_A）
    # 这份日志大多数英雄没有护甲，按护甲推荐不了；按统计推荐范达尔·雷矛
    assert [(a.pick, a.basis) for a in tap.heroes] == [(94, "stats")]
