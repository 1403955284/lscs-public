"""选英雄的规则建议：只看护甲（官方给越强的英雄越少的护甲）。"""

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
