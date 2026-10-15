"""用户录像回放过悬浮面板的数据模型，钉住几个真实状态（面板精简设计文档第 9.2 节）。

接法和 lscs replay --advice 一样（StateTracker + Pipeline + AdviceRunner + 复盘），不读
config.toml、不调 AI；没有卡牌数据（cards=None），名字就是卡牌 ID，所以只看高亮的位置、徽章、
来源和行的结构，不看卡名。AI 的深度分析是按代码里的数据形状注入的假建议。
"""

import os
from datetime import datetime
from pathlib import Path

import pytest
from helpers import NEEDS_USER_LOG, USER_LOG, keep_away_from_real_files, replay_log

from lscs.advisor.models import Action, Advice, GamePlan, Plan
from lscs.overlay.model import OverlayModel, OverlayView, Target
from lscs.state.models import EventType, Phase

pytestmark = NEEDS_USER_LOG

DEEP_SUMMARY = "补身材并凑畸变"
# 第 9 回合注入的整局计划（整局计划与流派指南设计文档第 8.2 节）；没有卡牌数据，卡名就是卡牌 ID
NINE_PLAN = GamePlan(
    comp="畸变神明",
    tribe="ABERRATION",
    key_cards=("BG36_099", "BG36_108", "BG36_113"),
    tiers="第 8 回合 5 本",
    stage="保血",
    turn=9,
)


def _identity(card_id: str) -> str:
    return card_id


def drive(detail: bool = False) -> dict[str, OverlayView]:
    """回放整局，返回几个关键时刻的面板内容。detail：详细模式。"""
    import lscs.cli as cli
    from lscs.advisor.display import DisplayGroup
    from lscs.advisor.runner import AdviceRunner
    from lscs.state.pool import load_pool_config

    rules = cli._load_rules()
    pool = load_pool_config(cli.DEFAULT_POOL)
    model = OverlayModel(_identity, buy_cost=rules.buy_cost_for, detail=detail)
    reviewer = cli._reviewer(
        cli._review_config(),
        rules,
        _identity,
        model,
        None,
        None,
        sync=True,
        now=lambda: datetime(2026, 10, 11, 23, 20),
        save_history=False,
    )
    runner = AdviceRunner(rules, _identity, DisplayGroup(model, reviewer.tap), None, cards=None)
    handlers = [model.on_update, runner.on_update, reviewer.on_update, lambda s, e: reviewer.tick()]
    views: dict[str, OverlayView] = {}
    injected = False
    for state, events in replay_log(USER_LOG, pool):
        for handler in handlers:
            handler(state, events)
        types = {e.type for e in events}
        if state.phase == Phase.HERO_SELECT and EventType.HEROES_OFFERED in types:
            views.setdefault("hero", model.snapshot())
        elif state.phase == Phase.COMBAT and state.turn == 9:
            views["combat9"] = model.snapshot()  # 第 9 回合战斗的最后一个状态
        elif state.phase == Phase.GAME_OVER:
            views["game_over"] = model.snapshot()
        if state.phase != Phase.RECRUIT or not state.main_action or state.choice is not None:
            continue
        if state.turn == 5:
            views.setdefault("recruit5", model.snapshot())
        if state.turn != 9:
            continue
        if not injected and len(state.shop) >= 3:
            # 第 9 回合招募开始时的深度分析：买左数第 3 个（3 金的随从）
            injected = True
            buy = Action(type="buy", entity_id=state.shop[2].entity_id)
            plan = Plan(source="llm", actions=(buy,), summary=DEEP_SUMMARY)
            advice = Advice(state_seq=state.seq, turn=9, plan=plan, lane="deep")
            model.show_ai(state, advice, False)
            views["recruit9_start"] = model.snapshot()
            model.show_plan(state, NINE_PLAN, None)
            views["recruit9_planned"] = model.snapshot()
        elif injected and state.me.gold == 0:
            views.setdefault("recruit9_broke", model.snapshot())
    return views


SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}


def project_files() -> dict[str, int]:
    """项目目录下的文件（不含 .git、.venv 和缓存）和它们的修改时间。"""
    from lscs.config import PROJECT_ROOT

    files = {}
    for folder, dirs, names in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for file in names:
            path = Path(folder) / file
            files[str(path.relative_to(PROJECT_ROOT))] = path.stat().st_mtime_ns
    return files


@pytest.fixture(scope="module", params=[False, True], ids=["compact", "detail"])
def mode(request, tmp_path_factory):
    """每种模式回放一次。模块级的 fixture 比 conftest 的守卫（每个用例）先运行，所以这里自己把
    config.toml、复盘目录、统计缓存、面板位置换到临时目录，并记下读过哪些配置文件、项目目录
    前后有没有变。"""
    import lscs.cli as cli

    home = tmp_path_factory.mktemp("real-log-home")
    loaded = []
    before = project_files()
    with pytest.MonkeyPatch.context() as mp:
        keep_away_from_real_files(mp, home)
        real_load = cli.load_config

        def spy(path):
            loaded.append(Path(path))
            return real_load(path)

        mp.setattr(cli, "load_config", spy)
        views = drive(detail=request.param)
    after = project_files()
    return request.param, views, {"home": home, "loaded": loaded, "before": before, "after": after}


@pytest.fixture
def run(mode):
    return mode[2]


@pytest.fixture
def views(mode):
    return mode[1]


@pytest.fixture
def detail(mode):
    return mode[0]


def lines(view):
    return [line for block in view.blocks for line in block.lines]


def block(view, kind):
    return next(b for b in view.blocks if b.kind == kind)


def test_hero_select_points_at_the_least_armor(views, detail):
    view = views["hero"]
    assert view.next_step.target == Target(row="hero", index=1, count=4)  # 凯瑞尔，护甲最少
    assert view.source == "规则"
    if detail:  # "备选"每个英雄一行，★ 在一行上
        heroes = block(view, "heroes").lines
        assert len(heroes) == 4 and sum(line.text.count("★") for line in heroes) == 1
    else:
        (row,) = [line for line in lines(view) if line.label == "护甲"]
        assert row.text.count("★") == 1


def test_turn_5_upgrade_first(views, detail):
    view = views["recruit5"]
    assert view.next_step.target.button == "upgrade"
    assert view.next_step.badge == "1"
    assert view.source == "规则"
    if detail:  # "留意"里有下个对手
        assert any(line.text.startswith("下个对手") for line in block(view, "aside").lines)
    else:
        labels = [line.label for line in lines(view)]
        assert set(labels) <= {"然后", "目标", "方向"}  # 精简模式不写对手


def test_deep_plan_first_while_it_is_affordable(views):
    view = views["recruit9_start"]
    assert view.next_step.target == Target(row="shop", index=2, count=7)
    assert (view.next_step.badge, view.source) == ("1", "AI 深度")


def test_spent_gold_switches_to_the_rules_plan(views, detail):
    # 第 9 回合铸币花到 0：深度分析的第一步（买 3 金的随从）做不起了，换成规则的英雄技能
    # （面板精简设计文档第 2 节第 8 条）；深度分析的"目标"还在
    view = views["recruit9_broke"]
    assert view.next_step.target == Target(button="hero_power")
    assert view.next_step.badge == "1"
    assert view.source == "规则"
    goals = [line.text for line in lines(view) if line.role == "goal"]
    if detail:  # 整局计划和目标同级，一直留到回合结束
        assert goals == [f"目标：{DEEP_SUMMARY}", "计划：畸变神明（保血） · 核心 " + PLAN_KEYS]
    else:
        assert goals == [DEEP_SUMMARY]
    assert not any(line.role == "alt" for line in lines(view))  # 显示的是规则：没有"规则另议"


# 第 9 回合场上有蚀脑怪和残忍的精神鞭笞者：打 ✓
PLAN_KEYS = "BG36_099✓、BG36_108✓、BG36_113 · 第 8 回合 5 本"


def test_injected_plan_adds_one_line_and_keeps_the_next_step(views, detail):
    before, after = views["recruit9_start"], views["recruit9_planned"]
    assert after.next_step == before.next_step and after.source == before.source
    added = [line for line in lines(after) if line not in lines(before)]
    if detail:
        assert [(line.text, line.role) for line in added] == [
            ("计划：畸变神明（保血） · 核心 " + PLAN_KEYS, "goal")
        ]
    else:
        (line,) = added
        assert (line.label, line.role, line.max_lines) == ("计划", "aside", 1)
        assert line.text == "畸变神明 · 保血 · 核心 BG36_099✓、BG36_108✓、BG36_113"
    assert "计划" not in "".join(line.text + line.label for line in lines(views["combat9"]))


def test_combat(views, detail):
    view = views["combat9"]
    assert view.title.startswith("第 9 回合 · 战斗中")
    if detail:  # 所有对手：正在打的在最前，已淘汰的合成最后一行
        rows = block(view, "opponents").lines
        assert len(rows) == 7
        assert rows[0].text.startswith("对手 第1名")
        assert rows[-1].text.startswith("已淘汰：")
    else:  # 标题，至多一行"最强"
        assert len(view.blocks) <= 1 and len(lines(view)) <= 1
        assert all(line.label == "最强" for line in lines(view))


def test_game_over_review(views, detail):
    view = views["game_over"]
    assert view.title.startswith("对局结束")
    assert lines(view)
    saved = [line for line in lines(view) if line.text.startswith("已保存")]
    assert len(saved) == (1 if detail else 0)  # 文件路径只在详细模式（和终端）里


def test_replay_stays_out_of_the_project(run):
    # 回放读的配置、写的复盘都在临时目录里：不读项目目录下的 config.toml（里面有 Key），
    # 不往 reviews/ 写文件，项目目录里一个文件都不多、不改
    assert run["loaded"] and all(path.is_relative_to(run["home"]) for path in run["loaded"])
    assert list((run["home"] / "reviews").glob("*.md"))
    assert run["after"] == run["before"]
