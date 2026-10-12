"""种族和关键词的中文名，以及"选一个"的标题和选项的写法（时间线、终端、面板共用）。"""

from __future__ import annotations

from collections.abc import Callable

from lscs.state.models import Choice, ChoiceKind, ChoiceOption

Namer = Callable[[str], str]

TRIBE_ZH: dict[str, str] = {
    "BEAST": "野兽",
    "DEMON": "恶魔",
    "DRAGON": "龙",
    "ELEMENTAL": "元素",
    "MECHANICAL": "机械",
    "MURLOC": "鱼人",
    "NAGA": "纳迦",
    "PIRATE": "海盗",
    "QUILBOAR": "野猪人",
    "UNDEAD": "亡灵",
    "ABERRATION": "畸变",
    "ALL": "全种族",
}

KEYWORD_ZH: dict[str, str] = {
    "TAUNT": "嘲讽",
    "DIVINE_SHIELD": "圣盾",
    "REBORN": "复生",
    "POISONOUS": "剧毒",
    "VENOMOUS": "烈毒",
    "WINDFURY": "风怒",
    "STEALTH": "潜行",
}


def tribe_zh(race: str) -> str:
    return TRIBE_ZH.get(race, race)


def keyword_zh(keyword: str) -> str:
    return KEYWORD_ZH.get(keyword, keyword)


def armor_zh(armor: int) -> str:
    """备选英雄的护甲；日志里没有 ARMOR 标签（读成 0）时写"未知"。"""
    return f"护甲 {armor}" if armor > 0 else "护甲 未知"


# 发起者的卡牌 ID 不知道时的标题；黑暗之赐总是用这个（卡名是"酒馆战棋黑暗之赐[DNT]"）
CHOICE_TITLES: dict[ChoiceKind, str] = {
    ChoiceKind.HERO_POWER: "英雄技能",
    ChoiceKind.TRINKET: "饰品",
    ChoiceKind.DARK_GIFT: "黑暗之赐",
    ChoiceKind.TRIPLE: "三连奖励",
    ChoiceKind.DISCOVER: "发现",
}


def choice_title(choice: Choice, name: Namer) -> str:
    """标题：定罪、小型饰品、黑暗之赐、三连奖励，其他用发起者的卡名。"""
    if choice.kind == ChoiceKind.DARK_GIFT or not choice.source_card_id:
        return CHOICE_TITLES[choice.kind]
    return name(choice.source_card_id)


def choice_option_text(option: ChoiceOption, name: Namer, gifts: bool = True) -> str:
    """随从："名字 攻/血"（黑暗之赐再加〔赐福〕）；饰品："名字（N 金）"；其他只写名字。"""
    card = name(option.card_id)
    if option.card_type == "MINION" and option.atk is not None:
        golden = "(金)" if option.golden and "(金)" not in card else ""
        text = f"{card}{golden} {option.atk}/{option.health}"
        if gifts and option.gifts:
            text += "〔" + "、".join(name(g) for g in option.gifts) + "〕"
        return text
    if option.card_type == "BATTLEGROUND_TRINKET" and option.cost is not None:
        return f"{card}（{option.cost} 金）"
    return card
