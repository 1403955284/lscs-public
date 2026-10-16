"""用户真实对局的商店核心卡（流派指南自动更新设计文档第 2.3、5.1 节）：夹具里的 HSReplay 指南
（patch_date 10-01）和夹具里的卡牌数据。不联网，不读 config.toml。"""

from datetime import date

import pytest
from helpers import (
    NEEDS_GUIDES_FIXTURES,
    NEEDS_USER_LOG,
    USER_LOG,
    fixture_live_guides,
    guides_cards,
    recruit_at,
    replay_log,
)

from lscs.advisor.core_cards import core_marks
from lscs.advisor.models import GamePlan
from lscs.cli import DEFAULT_POOL
from lscs.guides.model import to_comps_file
from lscs.state.pool import load_pool_config

pytestmark = [NEEDS_USER_LOG, NEEDS_GUIDES_FIXTURES]
PLAN = GamePlan(comp="畸变神明", tribe="ABERRATION", key_cards=("蚀脑怪", "飘荡祭品"), turn=5)


@pytest.fixture(scope="module")
def updates():
    return replay_log(USER_LOG, load_pool_config(DEFAULT_POOL))


@pytest.fixture(scope="module")
def setup():
    cards = guides_cards()
    live = fixture_live_guides()
    assert live.patch_date == date(2026, 10, 1)
    return cards.name, to_comps_file(live, None)


def test_turn_9_marks_the_sewer_escapee(updates, setup):
    name, comps = setup
    state = recruit_at(updates, 9)
    assert state.tribes == frozenset({"ABERRATION", "DEMON", "DRAGON", "MURLOC", "PIRATE"})
    marks = core_marks(state, None, comps, name)
    assert marks.source == "guides"
    (mark,) = marks.marks
    assert (mark.index, mark.name) == (2, "下水道逃生鱼人")  # 左数第 3 个
    assert mark.why.startswith("鱼人·下水道逃生鱼人")


def test_turn_5_with_the_plan_marks_both_wandering_offerings(updates, setup):
    name, comps = setup
    marks = core_marks(recruit_at(updates, 5), PLAN, comps, name)
    assert marks.source == "plan"
    assert [(m.index, m.name, m.why) for m in marks.marks] == [
        (1, "飘荡祭品", "计划"),  # 左数第 2 个
        (3, "飘荡祭品", "计划"),  # 左数第 4 个
    ]


def test_turn_1_marks_nothing(updates, setup):
    name, comps = setup
    marks = core_marks(recruit_at(updates, 1), None, comps, name)
    assert marks is None or marks.marks == ()
