"""用户自己录制的一局（2026-10-11，build 253216，凯瑞尔·罗姆，第 3 名）验证新版客户端。"""

import pytest
from helpers import NEEDS_USER_LOG, USER_LOG

from lscs.cli import DEFAULT_POOL
from lscs.replay import read_log_lines
from lscs.state.models import EventType, Phase, TribesConfidence
from lscs.state.pool import load_pool_config

pytestmark = NEEDS_USER_LOG


@pytest.fixture(scope="module")
def updates():
    from lscs.pipeline import Pipeline
    from lscs.state.tracker import StateTracker

    result = []
    pipeline = Pipeline(
        StateTracker(load_pool_config(DEFAULT_POOL)), lambda s, e: result.append((s, e))
    )
    for line in read_log_lines(USER_LOG):
        pipeline.push(line)
    pipeline.flush()
    return result


def recruit(updates, turn):
    for state, events in updates:
        if any(e.type == EventType.RECRUIT_STARTED and e.turn == turn for e in events):
            return state
    raise AssertionError(f"没有第 {turn} 回合的招募开始")


def test_whole_game(updates):
    assert not any(state.uncertain for state, _ in updates)
    recruits = [e for _, events in updates for e in events if e.type == EventType.RECRUIT_STARTED]
    assert len(recruits) == 13
    final = updates[-1][0]
    assert (final.phase, final.final_place) == (Phase.GAME_OVER, 3)
    assert (final.me.hero_card_id, final.me.hero_power_card_id) == (
        "BG21_HERO_000",
        "BG21_HERO_000p",
    )


def test_trinket_placeholders_are_not_trinkets(updates):
    assert recruit(updates, 1).me.trinkets == ()
    assert recruit(updates, 6).me.trinkets == ("BG36_MagicItem_402",)
    assert updates[-1][0].me.trinkets == ("BG36_MagicItem_402", "BG36_MagicItem_416")


def test_old_god_is_a_global_mechanic(updates):
    assert recruit(updates, 1).global_dbfs == (132532,)


def test_tavern_spell_cost_is_read_from_shop(updates):
    shop = recruit(updates, 1).shop
    assert [(s.card_id, s.card_type, s.cost) for s in shop] == [
        ("BG26_135", "MINION", None),
        ("BG36_098", "MINION", None),
        ("BG36_110", "MINION", None),
        ("BG28_897", "BATTLEGROUND_SPELL", 1),
    ]
    assert recruit(updates, 3).shop[-1].cost == 2


def test_tribes_confirmed_by_turn_3(updates):
    state = recruit(updates, 3)
    assert state.tribes == {"ABERRATION", "DEMON", "DRAGON", "MURLOC", "PIRATE"}
    assert state.tribes_confidence == TribesConfidence.CONFIRMED


def test_turn_6_starts_after_trinket_choice(updates):
    # 回合开始先选饰品（扣 4 金）和英雄技能姿态，选完才写入招募开始
    state = recruit(updates, 6)
    assert (state.me.gold, state.me.tavern_tier) == (4, 4)
    assert [(h.card_id, h.races) for h in state.me.hand] == [("BG36_108", ("ABERRATION",))]


def test_shop_keywords(updates):
    shop = recruit(updates, 13).shop
    assert shop[0].keywords == ("DIVINE_SHIELD", "WINDFURY")


def test_hero_power_usable_at_each_recruit_start_then_used(updates):
    # 凯瑞尔的"定罪"0 费、不需要目标，这局每回合都用了
    for turn in range(1, 14):
        hp = recruit(updates, turn).me.hero_power
        assert (hp.card_id, hp.cost, hp.option, hp.used_this_turn, hp.needs_target) == (
            "BG21_HERO_000p",
            0,
            "usable",
            False,
            False,
        ), turn
        later = [
            s.me.hero_power
            for s, _ in updates
            if s.phase == Phase.RECRUIT and s.turn == turn and s.me.hero_power.used_this_turn
        ]
        assert later, turn
        assert later[-1].option == "unusable"
        assert later[-1].unusable_reason == "REQ_NOT_EXHAUSTED_HERO_POWER"


def test_heroes_offered_once_before_the_choice(updates):
    order = [e.type for _, events in updates for e in events]
    assert order.count(EventType.HEROES_OFFERED) == 1
    offered_at = order.index(EventType.HEROES_OFFERED)
    assert order.index(EventType.GAME_STARTED) < offered_at < order.index(EventType.HERO_CHOSEN)
    state, event = next(
        (s, e) for s, events in updates for e in events if e.type == EventType.HEROES_OFFERED
    )
    assert event.data == {"count": 4, "changed": 0}
    select = state.hero_select
    # 按 ZONE_POSITION（游戏里从左到右）排序：创建后洗过一次牌，92 排到了第一个
    assert [(h.entity_id, h.card_id, h.armor) for h in select.offered] == [
        (92, "TB_BaconShop_HERO_91", 17),
        (89, "BG21_HERO_000", 10),
        (90, "BG28_HERO_800", 18),
        (91, "BG33_HERO_001", 19),
    ]
    assert [h.position for h in select.offered] == [1, 2, 3, 4]
    assert [h.hero_power_dbf for h in select.offered] == [64486, 73941, 105432, 129164]
    assert {h.entity_id for h in select.offered if h.locked} == {91, 92}
    assert {h.health for h in select.offered} == {30}
    assert select.reroll is True


def test_hero_select_cleared_once_chosen(updates):
    chosen = next(
        i
        for i, (_, events) in enumerate(updates)
        if any(e.type == EventType.HERO_CHOSEN for e in events)
    )
    assert all(state.hero_select is None for state, _ in updates[chosen:])
    assert recruit(updates, 1).hero_select is None
