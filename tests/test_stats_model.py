"""解析 Firestone 的统计（本版本统计设计文档第 5 节）：用裁剪过的夹具，不碰网络。"""

import json
from datetime import UTC, datetime

import pytest
from helpers import FIXTURES, NEEDS_STATS_FIXTURES

from lscs.config import StatsConfig
from lscs.stats.model import (
    MIN_CARD_PLAYS,
    MIN_HERO_POINTS,
    MIN_TRINKET_POINTS,
    MIN_TURN_PLAYS,
    load_meta_stats,
)

STATS = FIXTURES / "stats"
HERO, TRINKET, CARD = (
    STATS / "hero-stats.json",
    STATS / "trinket-stats.json",
    STATS / "card-stats.json",
)


ON = StatsConfig(enabled=True)
pytestmark = NEEDS_STATS_FIXTURES  # 公开版没有统计夹具：整个模块跳过


def load(cfg=ON, card=CARD, tz=UTC):
    return load_meta_stats(HERO, TRINKET, card, cfg, tz=tz)


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_thresholds():
    assert (MIN_HERO_POINTS, MIN_TRINKET_POINTS, MIN_CARD_PLAYS, MIN_TURN_PLAYS) == (
        1000,
        500,
        2000,
        200,
    )


def test_top_level_numbers():
    stats = load()
    assert (stats.period, stats.mmr, stats.games) == ("last-patch", 100, 938474)
    assert (len(stats.heroes), len(stats.trinkets), stats.hero_total) == (11, 10, 10)
    assert stats.cached_age is None
    # 三个文件里最早的更新时间（饰品、随从 00:10，英雄 03:10）
    assert stats.updated == datetime(2026, 10, 14, 0, 10, 27, 394000, tzinfo=UTC)


def test_hero_pick_rate_and_rank():
    stats = load()
    cariel = stats.hero("BG21_HERO_000")
    assert cariel.card_id == "BG21_HERO_000"
    assert (cariel.data_points, cariel.average_position, cariel.low) == (11258, 3.67, False)
    assert cariel.pick_rate == pytest.approx(11210 / 39531)
    assert cariel.rank == 2  # 夹具里只有加拉克苏斯大王更好
    assert stats.hero("TB_BaconShop_HERO_37").rank == 1
    # 平均名次相同（4.04）时按卡牌 ID 排
    assert stats.hero("TB_BaconShop_HERO_23").rank == 6
    assert stats.hero("TB_BaconShop_HERO_91").rank == 7
    assert stats.hero("TB_BaconShop_HERO_55").rank == 10


def test_low_sample_hero_has_no_rank_and_is_not_counted():
    stats = load()
    low = stats.hero("BG99_HERO_LOW")
    assert (low.data_points, low.average_position, low.low, low.rank) == (400, 4.2, True, None)
    assert low.pick_rate == pytest.approx(0.2)
    assert stats.hero_total == 10


def test_hero_lookup_strips_skin_suffix():
    stats = load()
    millhouse = stats.hero("TB_BaconShop_HERO_49_SKIN_A")
    assert millhouse is not None and millhouse.card_id == "TB_BaconShop_HERO_49"
    assert stats.hero("BG21_HERO_000_SKIN_C2").card_id == "BG21_HERO_000"
    assert stats.hero("TB_BaconShop_HERO_99") is None


def test_hero_median_counts_only_enough_samples():
    # 3.55 3.67 3.89 3.91 3.99 | 4.04 4.04 4.06 4.08 4.45（样本少的 4.20 不算）
    assert load().hero_median == pytest.approx((3.99 + 4.04) / 2)


def test_trinkets_use_the_configured_bracket():
    stats = load()
    portrait = stats.trinket("BG36_MagicItem_402")
    assert (portrait.data_points, portrait.average_placement, portrait.low) == (8835, 3.89, False)
    assert portrait.pick_rate == pytest.approx(0.26547475961538464)
    top_half = load(StatsConfig(enabled=True, mmr_percentile=50)).trinket("BG36_MagicItem_402")
    assert (top_half.data_points, top_half.average_placement) == (3252, 4.04)
    assert top_half.pick_rate == pytest.approx(0.20354259247668524)
    assert stats.trinket("BG30_MagicItem_420").average_placement == 3.95  # 3.9451，保留两位


def test_trinket_lookup_tries_without_the_trailing_t():
    stats = load()
    assert stats.trinket("BG30_MagicItem_426t").data_points == 36068  # 带 t 的那条有自己的数据
    assert stats.trinket("BG30_MagicItem_426").data_points == 23115
    assert stats.trinket("BG36_MagicItem_402t").card_id == "BG36_MagicItem_402"
    assert stats.trinket("BG36_MagicItem_999") is None


def test_low_sample_trinket():
    low = load().trinket("BG99_MagicItem_LOW")
    assert (low.data_points, low.average_placement, low.low) == (300, 3.5, True)


def test_trinket_falls_back_to_the_overall_numbers(tmp_path):
    trinket = {
        "lastUpdateDate": "2026-10-14T00:10:27Z",
        "trinketStats": [
            {
                "trinketCardId": "T1",
                "dataPoints": 900,
                "pickRate": 0.3,
                "averagePlacement": 3.777,
                "averagePlacementAtMmr": [{"mmr": 100, "dataPoints": 900, "placement": 3.777}],
                "pickRateAtMmr": [{"mmr": 100, "dataPoints": 900, "pickRate": 0.3}],
            }
        ],
    }
    path = write(tmp_path, "trinket.json", trinket)
    cfg = StatsConfig(enabled=True, mmr_percentile=10)  # 条目里没有前 10% 的那条
    t1 = load_meta_stats(HERO, path, None, cfg).trinket("T1")
    assert (t1.data_points, t1.average_placement, t1.pick_rate) == (900, 3.78, 0.3)


def turn(n, played, mine, other):
    return {
        "turn": n,
        "totalPlayed": played,
        "averagePlacement": mine,
        "totalOther": played * 5,
        "averagePlacementOther": other,
    }


def test_card_impact_is_turn_weighted(tmp_path):
    card = {
        "lastUpdateDate": "2026-10-14T00:10:31Z",
        "cardStats": [
            {
                "cardId": "C1",
                "totalPlayed": 5000,
                "averagePlacement": 3.0,
                "averagePlacementOther": 4.5,
                "turnStats": [
                    turn(3, 1000, 4.0, 4.5),  # +0.5
                    turn(4, 3000, 3.8, 4.0),  # +0.2
                    turn(5, 1000, 3.5, 4.4),  # +0.9
                    turn(6, 199, 1.0, 8.0),  # 不到 200 次：不算
                    turn(7, 800, 2.0, None),  # 没打的玩家的平均名次是 null：不算
                ],
            },
            # 一个回合都不够：不收录
            {"cardId": "C2", "totalPlayed": 150, "turnStats": [turn(3, 150, 3.0, 4.0)]},
            # 收录，但打出太少
            {"cardId": "C3", "totalPlayed": 1999, "turnStats": [turn(3, 1999, 3.0, 4.0)]},
        ],
    }
    stats = load_meta_stats(HERO, TRINKET, write(tmp_path, "card.json", card), StatsConfig())
    c1 = stats.card("C1")
    # (1000×0.5 + 3000×0.2 + 1000×0.9) / 5000 = 0.4（原始差值是 1.5）
    assert (c1.played, c1.impact, c1.low) == (5000, 0.4, False)
    assert stats.card("C2") is None
    assert (stats.card("C3").impact, stats.card("C3").low) == (1.0, True)


def test_cards_from_the_fixture():
    stats = load()
    assert set(stats.cards) == {
        "BG20_100",
        "BG31_326",
        "BG30_123",
        "BG23_318",
        "BG36_312",
        "BG36_356",
        "BG36_372",
        "BG99_LOW",  # 只打出 50 次的 BG99_RARE 不收录
    }
    assert stats.card("BG99_LOW").low and not stats.card("BG30_123").low
    # 莽神火车王：原始差值 1.40，按回合比较后很小（"后期才买得起"）
    assert stats.card("BG23_318").impact < 0.2


def test_cards_switched_off():
    stats = load(card=None)
    assert stats.cards == {} and stats.card("BG20_100") is None


def test_source_text_and_panel_source():
    stats = load()
    assert stats.source_text() == "数据来自 Firestone · 本补丁 · 全部玩家 · 10-14 更新"
    assert stats.panel_source() == "Firestone"
    week = stats.model_copy(update={"period": "past-seven", "mmr": 25})
    assert week.source_text() == "数据来自 Firestone · 最近 7 天 · 前 25% · 10-14 更新"


def test_broken_entries_are_skipped(tmp_path):
    hero = {
        "lastUpdateDate": "2026-10-14T03:10:27.372Z",
        "dataPoints": 10,
        "heroStats": [
            {
                "heroCardId": "H1",
                "dataPoints": 5000,
                "totalOffered": 10,
                "totalPicked": 5,
                "averagePosition": 3.9,
            },
            {
                "heroCardId": "H2",
                "dataPoints": "很多",
                "totalOffered": 10,
                "totalPicked": 5,
                "averagePosition": 3.9,
            },
            {"heroCardId": "H3"},
            "not a hero",
            {
                "heroCardId": "H4",
                "dataPoints": 5000,
                "totalOffered": 0,
                "totalPicked": 0,
                "averagePosition": 4.1,
            },
        ],
    }
    stats = load_meta_stats(write(tmp_path, "hero.json", hero), TRINKET, None, StatsConfig())
    assert set(stats.heroes) == {"H1", "H4"}
    assert stats.hero("H4").pick_rate == 0  # 没被提供过
    assert stats.hero_total == 2


@pytest.mark.parametrize("which", ["hero", "trinket", "card"])
def test_missing_top_level_key_raises(tmp_path, which):
    bad = write(tmp_path, "bad.json", {"lastUpdateDate": "2026-10-14T03:10:27Z"})
    paths = {"hero": HERO, "trinket": TRINKET, "card": CARD, which: bad}
    with pytest.raises(ValueError):
        load_meta_stats(paths["hero"], paths["trinket"], paths["card"], StatsConfig())


def test_times_are_shown_in_the_given_timezone():
    from datetime import timedelta, timezone

    beijing = timezone(timedelta(hours=8))
    stats = load(tz=beijing)
    assert stats.updated.tzinfo == UTC  # 内部照旧是 UTC
    assert f"{stats.local_updated:%m-%d %H:%M}" == "10-14 08:10"
    late = {"updated": datetime(2026, 10, 13, 20, 0, tzinfo=UTC)}
    assert stats.model_copy(update=late).date_text == "10-14"  # 北京时间已经是第二天
    assert load().model_copy(update=late).date_text == "10-13"


def test_missing_bracket_falls_back_with_a_debug_log(tmp_path, caplog):
    import logging

    trinket = {
        "lastUpdateDate": "2026-10-14T00:10:27Z",
        "trinketStats": [
            {
                "trinketCardId": "T1",
                "dataPoints": 900,
                "pickRate": 0.3,
                "averagePlacement": 3.7,
                "averagePlacementAtMmr": [],
                "pickRateAtMmr": [],
            }
        ],
    }
    path = write(tmp_path, "trinket.json", trinket)
    with caplog.at_level(logging.DEBUG, logger="lscs.stats.model"):
        load_meta_stats(HERO, path, None, StatsConfig(mmr_percentile=25))
    assert "T1" in caplog.text and "前 25%" in caplog.text
