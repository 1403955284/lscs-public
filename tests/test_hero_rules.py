"""选英雄的规则建议：只看护甲（官方给越强的英雄越少的护甲）。"""

from helpers import NEEDS_STATS_FIXTURES

from lscs.advisor.hero_rules import ARMOR_MISSING, LOWEST_ARMOR, rule_hero_pick
from lscs.advisor.models import HeroComment
from lscs.state.models import HeroOption, HeroSelect


def option(entity_id, armor, position=None, card_id=None):
    return HeroOption(
        entity_id=entity_id,
        card_id=card_id or f"HERO_{entity_id}",
        position=position if position is not None else entity_id % 10,
        armor=armor,
        health=30,
        hero_power_dbf=None,
    )


def select(*options):
    return HeroSelect(offered=tuple(options))


def test_lowest_armor_is_picked():
    heroes = select(option(92, 17, 1), option(89, 10, 2), option(90, 18, 3), option(91, 19, 4))
    advice = rule_hero_pick(heroes)
    assert (advice.source, advice.pick) == ("rules", 89)
    assert advice.reason == LOWEST_ARMOR == "这几个英雄里它护甲最少：官方给越强的英雄发越少的护甲"
    assert advice.signature == heroes.signature()
    assert advice.comments == (
        HeroComment(entity_id=92, text="护甲 17"),
        HeroComment(entity_id=89, text="护甲 10"),
        HeroComment(entity_id=90, text="护甲 18"),
        HeroComment(entity_id=91, text="护甲 19"),
    )
    assert advice.elapsed is None


def test_tie_goes_to_the_leftmost():
    heroes = select(option(1, 12, 3), option(2, 12, 1), option(3, 15, 2), option(4, 18, 4))
    assert rule_hero_pick(heroes).pick == 2


def test_missing_armor_means_no_pick():
    # 2022 年的日志：大多数英雄没有 ARMOR 标签
    heroes = select(option(92, 5, 1), option(91, 0, 2), option(93, 0, 3), option(94, 10, 4))
    advice = rule_hero_pick(heroes)
    assert (advice.pick, advice.reason) == (None, ARMOR_MISSING)
    assert ARMOR_MISSING == "护甲信息不全，等 AI 建议"
    assert [c.text for c in advice.comments] == ["护甲 5", "护甲 未知", "护甲 未知", "护甲 10"]


def test_same_armor_means_no_pick():
    four = rule_hero_pick(select(*(option(i, 15, i) for i in range(1, 5))))
    assert (four.pick, four.reason) == (None, "四个英雄护甲一样，等 AI 建议")
    two = rule_hero_pick(select(option(1, 15, 1), option(2, 15, 2)))
    assert (two.pick, two.reason) == (None, "备选英雄护甲都一样，等 AI 建议")


# ---- 本版本统计（本版本统计设计文档第 6.2 节） ----

USER_HEROES = (  # 用户那局的四个（画面顺序）：杰弗里斯、凯瑞尔、泰瑟兰、洛
    (92, "TB_BaconShop_HERO_91", 17),
    (89, "BG21_HERO_000", 10),
    (90, "BG28_HERO_800", 18),
    (91, "BG33_HERO_001", 19),
)


def user_select(heroes=USER_HEROES):
    return select(
        *(option(e, armor, i, card_id=c) for i, (e, c, armor) in enumerate(heroes, start=1))
    )


@NEEDS_STATS_FIXTURES
def test_stats_pick_the_best_average_placement():
    from helpers import fixture_stats

    heroes = user_select()
    advice = rule_hero_pick(heroes, fixture_stats())
    assert (advice.source, advice.pick, advice.basis) == ("rules", 89, "stats")
    assert advice.reason == (
        "这几个英雄里它本版本平均名次最好（第 3.67 名，10 个英雄里第 2；数据来自 Firestone）"
    )
    # 夹具里样本够的英雄有 10 个，排名的分母是 10
    assert advice.comments == (
        HeroComment(entity_id=92, text="第 4.04 名 · 选 27% · 第 7/10"),
        HeroComment(entity_id=89, text="第 3.67 名 · 选 28% · 第 2/10"),
        HeroComment(entity_id=90, text="第 3.99 名 · 选 15% · 第 5/10"),
        HeroComment(entity_id=91, text="第 3.91 名 · 选 29% · 第 4/10"),
    )


@NEEDS_STATS_FIXTURES
def test_stats_tie_goes_to_less_armor_then_the_leftmost():
    from helpers import fixture_stats

    # 沙德沃克和杰弗里斯都是 4.04
    tie = ((1, "TB_BaconShop_HERO_23", 15), (2, "TB_BaconShop_HERO_91", 12))
    assert rule_hero_pick(user_select(tie), fixture_stats()).pick == 2
    same = ((1, "TB_BaconShop_HERO_23", 12), (2, "TB_BaconShop_HERO_91", 12))
    assert rule_hero_pick(user_select(same), fixture_stats()).pick == 1


@NEEDS_STATS_FIXTURES
def test_a_low_sample_or_unknown_hero_falls_back_to_armor():
    from helpers import fixture_stats

    stats = fixture_stats()
    for odd in ("BG99_HERO_LOW", "TB_BaconShop_HERO_999"):
        heroes = user_select((*USER_HEROES[:3], (91, odd, 19)))
        advice = rule_hero_pick(heroes, stats)
        assert advice == rule_hero_pick(heroes)  # 和没有统计时一字不差
        assert (advice.pick, advice.reason, advice.basis) == (89, LOWEST_ARMOR, "armor")
        assert advice.comments[0] == HeroComment(entity_id=92, text="护甲 17")


def test_without_stats_nothing_changes():
    heroes = user_select()
    advice = rule_hero_pick(heroes, None)
    assert advice == rule_hero_pick(heroes)
    assert (advice.pick, advice.reason, advice.basis) == (89, LOWEST_ARMOR, "armor")


@NEEDS_STATS_FIXTURES
def test_stats_recommend_a_hero_even_without_armor():
    # CC0 样本（2022）：大多数英雄没有护甲，按护甲给不出推荐；米尔豪斯是皮肤 ID
    from helpers import fixture_stats

    heroes = user_select(
        (
            (92, "TB_BaconShop_HERO_23", 5),
            (91, "TB_BaconShop_HERO_67", 0),
            (93, "TB_BaconShop_HERO_49_SKIN_A", 0),
            (94, "BG22_HERO_003", 10),
        )
    )
    assert rule_hero_pick(heroes).pick is None
    advice = rule_hero_pick(heroes, fixture_stats())
    assert (advice.pick, advice.basis) == (94, "stats")  # 范达尔·雷矛 3.89
    assert advice.comments[2].text == "第 4.08 名 · 选 10% · 第 9/10"
