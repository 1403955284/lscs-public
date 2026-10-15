"""整局计划的写法（提示词、终端、面板共用；整局计划与流派指南设计文档第 7.2、8 节），以及
快速方案偏离计划时的提醒（第 5.2 节）。

核心卡后面的 ✓ 表示场面或手牌里已经有同名随从（含金色）。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from lscs.advisor.context import MINION, TurnContext, is_golden
from lscs.advisor.models import GamePlan, Plan
from lscs.advisor.names import tribe_zh
from lscs.cards.db import GOLDEN_MARKS
from lscs.state.models import GameState

Namer = Callable[[str], str]
HAVE = "✓"  # 已经有的核心卡
HAVE_MARKS = ("✓", "✔")  # AI 把局面里的 ✓ 抄回来时也要去掉


def plain_name(text: str) -> str:
    """卡名去掉 ✓ 和末尾的"(金)"。"""
    for mark in HAVE_MARKS:
        text = text.replace(mark, "")
    text = text.strip()
    for golden in GOLDEN_MARKS:
        text = text.removesuffix(golden).strip()
    return text


def held_names(state: GameState | None, name: Namer) -> frozenset[str]:
    """场面和手牌里的随从的卡名（金色的也写成普通卡名）。"""
    me = state.me if state is not None else None
    if me is None:
        return frozenset()
    cards = [*me.board, *(h for h in me.hand if h.card_type == MINION)]
    return frozenset(plain_name(name(c.card_id)) for c in cards if c.card_id)


def key_cards_text(plan: GamePlan, state: GameState | None, name: Namer) -> str:
    """核心卡的写法，如"蚀脑怪✓、残忍的精神鞭笞者、飘荡祭品"；没有核心卡时为空。"""
    held = held_names(state, name)
    return "、".join(f"{card}{HAVE if card in held else ''}" for card in plan.key_cards)


def comp_with_tribe(plan: GamePlan, always: bool = False) -> str:
    """目标流派，主种族写在后面的括号里："海盗刀剑（海盗）"。always 为假时，流派名里已经带着
    种族名（"畸变神明"）就不重复写。"未定"不带种族。"""
    if plan.tribe is None or plan.undecided:
        return plan.comp
    tribe = tribe_zh(plan.tribe)
    if tribe == plan.comp or (not always and tribe in plan.comp):
        return plan.comp
    return f"{plan.comp}（{tribe}）"


def plan_look_for(look_for: Sequence[str], game_plan: GamePlan | None) -> tuple[str, ...]:
    """要找的牌加上整局计划的核心卡（去重保序）：张数估计也写核心卡还剩几张。"""
    names = list(look_for)
    for card in game_plan.key_cards if game_plan is not None else ():
        if card not in names:
            names.append(card)
    return tuple(names)


def _plan_spending(plan: Plan, ctx: TurnContext) -> tuple[int, int]:
    """方案自己要花的铸币（买、刷新、英雄技能；卖一张 +1）和执行完后场面 + 手牌的随从数。"""
    shop = {item.entity_id: item for item in ctx.shop}
    spent, minions = 0, ctx.board_minions + ctx.hand_minions
    for action in plan.actions:
        if action.type == "buy" and action.entity_id in shop:
            item = shop[action.entity_id]
            spent += ctx.price(item)
            minions += item.card_type == MINION
        elif action.type == "sell":
            spent -= 1
            minions -= 1
        elif action.type == "refresh":
            spent += ctx.refresh_cost or 0
        elif action.type == "hero_power" and ctx.hero_power is not None:
            spent += ctx.hero_power.cost
    return spent, minions


def off_plan_note(
    plan: Plan, game_plan: GamePlan | None, ctx: TurnContext, state: GameState, name: Namer
) -> str | None:
    """方案和计划明显矛盾时的一句提醒；没有计划、没有矛盾时 None。只查一种情况：商店里有计划的
    核心卡（卡名相同、不是金色），方案一步都没买它，而且照方案做完其他步之后还买得起、还放得下。
    不算矛盾的：已经有这张核心卡（场面或手牌）；方案里有升本（铸币留给升本）。几张都是这样时
    只提第一张（按计划里的顺序）。"""
    if game_plan is None or not game_plan.key_cards:
        return None
    if any(a.type == "upgrade" for a in plan.actions):
        return None
    spent, minions = _plan_spending(plan, ctx)
    if minions >= ctx.minion_limit:
        return None  # 照方案做完场面就满了
    left = ctx.gold - spent
    held = held_names(state, name)
    bought = {a.entity_id for a in plan.actions if a.type == "buy"}
    for card in game_plan.key_cards:
        if card in held:
            continue
        items = [
            item
            for item in ctx.shop
            if item.card_type == MINION
            and not is_golden(item.card_id, item.golden)
            and plain_name(name(item.card_id)) == card
        ]
        if any(item.entity_id in bought for item in items):
            continue
        if any(ctx.price(item) <= left for item in items):
            return f"商店里有核心卡 {card}，方案却没买它"
    return None
