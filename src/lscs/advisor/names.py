"""种族和关键词的中文名。"""

from __future__ import annotations

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
