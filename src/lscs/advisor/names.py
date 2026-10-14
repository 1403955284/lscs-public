"""种族和关键词的中文名，随从名（金色加"(金)"），"选一个"的标题和选项的写法
（时间线、终端、面板共用），以及血量和战斗结果的写法（时间线、棋盘顶部的横幅）。"""

from __future__ import annotations

from collections.abc import Callable

from lscs.state.models import Choice, ChoiceKind, ChoiceOption, CombatResult, Minion

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


def minion_label(m: Minion, name: Namer) -> str:
    """随从的名字，金色的加"(金)"（卡名里已经带着就不再加）。"""
    card = name(m.card_id)
    return f"{card}(金)" if m.golden and "(金)" not in card else card


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


OUTCOME_ZH = {"win": "赢", "loss": "输", "tie": "平"}
RESULT_HEAD = "本场已定："  # 真实结果、不是预测：战斗一开始日志里就写完了


def hp_text(health: int, armor: int) -> str:
    """血量的写法：有护甲 "30+8"，没有 "19"，淘汰了 "-14"。"""
    return f"{health}+{armor}" if armor > 0 else str(health)


def _dealt_known(result: CombatResult) -> bool:
    """量到了对手掉的血：不是残影、对手认得出、确实掉了血（赢了却是 0 说明没量到）。"""
    return (
        not result.ghost
        and result.opponent_hero is not None
        and result.opponent_health is not None
        and result.dealt > 0
    )


def combat_result_line(result: CombatResult, name: Namer) -> str:
    """时间线里的一行（提前显示战斗结果设计文档第 5.1 节），如"⚔ 本场已定：赢 · 洛卡拉掉 5 血
    （剩 30+9）"。赢：残影写"打的是残影"，对手认不出只写"赢"；输：写我方掉血；平：双方都没掉血。"""
    head = f"⚔ {RESULT_HEAD}{OUTCOME_ZH[result.outcome]}"
    if result.outcome == "win":
        if result.ghost:
            return f"{head} · 打的是残影，不掉血"
        if not _dealt_known(result):
            return head
        hp = hp_text(result.opponent_health or 0, result.opponent_armor or 0)
        text = f"{head} · {name(result.opponent_hero or '')}掉 {result.dealt} 血（剩 {hp}）"
        return f"{text} · 对手被淘汰" if result.opponent_dead else text
    if result.outcome == "loss":
        hp = hp_text(result.my_health, result.my_armor)
        text = f"{head} · 你掉 {result.lost} 血（剩 {hp}）"
        return f"{text} · 你被淘汰" if result.me_dead else text
    return f"{head} · 双方都没掉血"


def combat_banner_text(result: CombatResult, name: Namer) -> str:
    """棋盘顶部横幅的一行字（提前显示战斗结果设计文档第 5.3 节）：和时间线同一套规则，去掉
    "（剩 …）"，尽量一行放得下，如"本场已定：赢 · 洛卡拉掉 5 血"。输了没淘汰时写"，剩 30+8"。"""
    head = f"{RESULT_HEAD}{OUTCOME_ZH[result.outcome]}"
    if result.outcome == "win":
        if result.ghost:
            return f"{head} · 残影，不掉血"
        if not _dealt_known(result):
            return head
        text = f"{head} · {name(result.opponent_hero or '')}掉 {result.dealt} 血"
        return f"{text} · 对手淘汰" if result.opponent_dead else text
    if result.outcome == "loss":
        if result.me_dead:
            return f"{head} · 你掉 {result.lost} 血 · 你被淘汰"
        return f"{head} · 你掉 {result.lost} 血，剩 {hp_text(result.my_health, result.my_armor)}"
    return f"{head} · 都没掉血"
