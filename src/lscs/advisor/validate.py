"""校验一个方案能否在当前局面下按顺序执行。规则方案和 AI 方案共用。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from lscs.advisor.context import MINION, TurnContext, is_golden
from lscs.advisor.models import Action
from lscs.state.pool import base_card_id


def validate_plan(actions: Sequence[Action], ctx: TurnContext) -> str | None:
    """合法返回 None；否则返回第一条不满足的原因（中文）。"""
    shop = {item.entity_id: item for item in ctx.shop}
    board = {m.entity_id: m for m in ctx.board}
    gold = ctx.gold
    minions = ctx.board_minions + ctx.hand_minions  # 假设手牌里的随从都会打出
    copies = Counter(ctx.copies)
    bought: set[int] = set()
    sold: set[int] = set()
    upgraded = refreshed = frozen = False

    for step, action in enumerate(actions, start=1):
        where = f"第 {step} 步"
        if frozen:
            return f"{where}：冻结之后不能再有动作"
        if refreshed and action.type != "freeze":
            return f"{where}：刷新之后只能冻结"
        match action.type:
            case "buy":
                item = shop.get(action.entity_id)  # type: ignore[arg-type]
                if item is None:
                    return f"{where}：商店里没有 #{action.entity_id}"
                if action.entity_id in bought:
                    return f"{where}：#{action.entity_id} 已经买过"
                bought.add(item.entity_id)
                gold -= ctx.price(item)
                if item.card_type == MINION:
                    minions += 1
                    if not is_golden(item.card_id, item.golden):
                        base = base_card_id(item.card_id)
                        copies[base] += 1
                        if copies[base] >= 3:  # 三张合成一张金色
                            copies[base] -= 3
                            minions -= 2
            case "sell":
                minion = board.get(action.entity_id)  # type: ignore[arg-type]
                if minion is None:
                    return f"{where}：场上没有 #{action.entity_id}"
                if action.entity_id in sold:
                    return f"{where}：#{action.entity_id} 已经卖过"
                sold.add(minion.entity_id)
                gold += 1
                minions -= 1
                if not is_golden(minion.card_id, minion.golden):
                    copies[base_card_id(minion.card_id)] -= 1
            case "upgrade":
                if ctx.upgrade_cost is None:
                    return f"{where}：已经是最高等级，不能升本"
                if upgraded:
                    return f"{where}：一个方案只能升本一次"
                upgraded = True
                gold -= ctx.upgrade_cost
            case "refresh":
                if ctx.refresh_cost is None:
                    return f"{where}：现在不能刷新"
                refreshed = True
                gold -= ctx.refresh_cost
            case "freeze":
                frozen = True
        if gold < 0:
            return f"{where}：铸币不够"
    if minions > ctx.minion_limit:
        return f"场面放不下：执行后会有 {minions} 个随从（最多 {ctx.minion_limit} 个）"
    return None
