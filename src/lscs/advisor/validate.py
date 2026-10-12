"""校验一个方案能否在当前局面下按顺序执行。规则方案和 AI 方案共用。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from lscs.advisor.context import MINION, TurnContext, hero_power_blocker, is_golden
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
    upgraded = refreshed = frozen = powered = False

    for step, action in enumerate(actions, start=1):
        where = f"第 {step} 步"
        if frozen:
            return f"{where}：冻结之后不能再有动作"
        if refreshed:
            # 刷新之后看不到新商店：冻结一个还没看到的商店也没有意义（提示词第 3 条）
            return f"{where}：刷新要放在最后，刷新之后不能再有动作"
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
            case "hero_power":
                if powered:
                    return f"{where}：一个方案只能用一次英雄技能"
                problem = hero_power_blocker(ctx.hero_power)
                if problem is not None:
                    return f"{where}：{problem}"
                hp = ctx.hero_power
                assert hp is not None
                if hp.needs_target is not False:  # 确定不需要目标时忽略 entity_id
                    target = action.entity_id
                    if target is None:
                        return f"{where}：英雄技能要选一个目标"
                    if target in sold:
                        return f"{where}：#{target} 已经卖掉了"
                    if hp.option == "usable":
                        if target not in hp.targets:
                            return f"{where}：英雄技能不能以 #{target} 为目标"
                    elif target not in board:
                        # 铸币不够时选项不列目标：只能选场上的随从
                        return f"{where}：英雄技能只能以场上的随从为目标，#{target} 不在场上"
                powered = True
                gold -= hp.cost
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


def normalize_actions(actions: Sequence[Action], ctx: TurnContext) -> tuple[Action, ...]:
    """去掉确定不需要目标的英雄技能步骤上的 entity_id（校验时本来就忽略），
    这样和规则方案一样的 AI 方案才会被认作"同意规则建议"。"""
    hp = ctx.hero_power
    if hp is None or hp.needs_target is not False:
        return tuple(actions)
    return tuple(
        a.model_copy(update={"entity_id": None})
        if a.type == "hero_power" and a.entity_id is not None
        else a
        for a in actions
    )
