"""精简的规则建议：三连、按节奏升本、按身材买牌、用掉免费的英雄技能、剩余铸币刷新
（M2 设计文档第 5 节；英雄技能见 2026-10-11 英雄技能建议设计）。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping

from lscs.advisor.context import MINION, TurnContext, hero_power_used, is_golden
from lscs.advisor.models import Action, Plan
from lscs.advisor.names import keyword_zh, tribe_zh
from lscs.advisor.rules_config import RulesConfig
from lscs.advisor.validate import validate_plan
from lscs.state.models import Minion, ShopItem
from lscs.state.pool import base_card_id

Namer = Callable[[str], str]
MAX_PLANS = 3


def stat_score(card: ShopItem | Minion, cfg: RulesConfig, main_tribe: str | None) -> int:
    """身材分：攻击 + 生命 + 关键词加分 + 主力种族加分；酒馆法术固定低分。"""
    if isinstance(card, ShopItem) and card.card_type != MINION:
        return cfg.spell_score
    score = (card.atk or 0) + (card.health or 0) + cfg.keyword_score * len(card.keywords)
    if main_tribe and main_tribe in card.races:
        score += cfg.tribe_score
    return score


def _completes_triple(copies: Mapping[str, int], item: ShopItem) -> bool:
    """买下这张后凑成三连：已有 2 张非金色的同名随从。"""
    if item.card_type != MINION or is_golden(item.card_id, item.golden):
        return False
    return copies.get(base_card_id(item.card_id), 0) == 2


class _Draft:
    """边模拟铸币、场面和张数，边生成动作。"""

    def __init__(self, ctx: TurnContext, cfg: RulesConfig, name: Namer) -> None:
        self.ctx, self.cfg, self.name = ctx, cfg, name
        self.gold = ctx.gold
        self.minions = ctx.board_minions + ctx.hand_minions
        self.copies: Counter[str] = Counter(ctx.copies)
        self.bought: set[int] = set()
        self.sold: set[int] = set()
        self.actions: list[Action] = []

    def score(self, card: ShopItem | Minion) -> int:
        return stat_score(card, self.cfg, self.ctx.main_tribe)

    def completes_triple(self, item: ShopItem) -> bool:
        return _completes_triple(self.copies, item)

    def buy(self, item: ShopItem, reason: str) -> None:
        self.gold -= self.ctx.price(item)
        self.bought.add(item.entity_id)
        self.minions += 1
        if not is_golden(item.card_id, item.golden):
            base = base_card_id(item.card_id)
            self.copies[base] += 1
            if self.copies[base] >= 3:
                self.copies[base] -= 3
                self.minions -= 2
        self.actions.append(Action(type="buy", entity_id=item.entity_id, reason=reason))

    def sell(self, minion: Minion) -> None:
        self.gold += 1
        self.sold.add(minion.entity_id)
        self.minions -= 1
        if not is_golden(minion.card_id, minion.golden):
            self.copies[base_card_id(minion.card_id)] -= 1
        reason = f"卖掉最弱的 {self.name(minion.card_id)}（{minion.atk}/{minion.health}）腾位置"
        self.actions.append(Action(type="sell", entity_id=minion.entity_id, reason=reason))

    def upgrade(self, reason: str) -> None:
        self.gold -= self.ctx.upgrade_cost or 0
        self.actions.append(Action(type="upgrade", reason=reason))

    def buy_reason(self, item: ShopItem, first: bool) -> str:
        parts = ["商店里身材最好" if first else "剩余铸币里身材最好"]
        if item.keywords:
            parts.append("有" + "、".join(keyword_zh(k) for k in item.keywords))
        main = self.ctx.main_tribe
        if main and main in item.races:
            parts.append(f"契合主力种族{tribe_zh(main)}")
        if self.copies[base_card_id(item.card_id)] == 1 and not is_golden(
            item.card_id, item.golden
        ):
            parts.append("凑成对子")
        return "，".join(parts)

    def fill_buys(self) -> None:
        """按身材分从高到低买到铸币用完；场面放不下时卖掉更弱的随从。"""
        first = not any(a.type == "buy" for a in self.actions)
        while True:
            options = [
                i for i in self.ctx.shop if i.card_type == MINION and i.entity_id not in self.bought
            ]
            if not options:
                return
            best = max(options, key=lambda i: (self.score(i), -i.position, -i.entity_id))
            if self.gold < self.ctx.price(best):
                return
            after = self.minions + 1 - (2 if self.completes_triple(best) else 0)
            if after > self.ctx.minion_limit:
                remaining = [m for m in self.ctx.board if m.entity_id not in self.sold]
                if not remaining:
                    return
                weakest = min(remaining, key=lambda m: (self.score(m), m.entity_id))
                if self.score(best) <= self.score(weakest):
                    return
                self.sell(weakest)
            self.buy(best, self.buy_reason(best, first))
            first = False

    def maybe_hero_power(self) -> None:
        """只安排现在能用、不需要目标、0 费的技能；付费或要选目标的交给 AI 判断。"""
        hp = self.ctx.hero_power
        if (
            hp is None
            or hp.option != "usable"
            or hero_power_used(hp)
            or hp.needs_target is not False
            or hp.cost != 0
        ):
            return
        if self.ctx.hand_minions > 0 or any(a.type == "buy" for a in self.actions):
            # 买来的随从在手牌里（本方案买的，或这回合早些时候买的）：
            # 给随从加属性的技能要等它打出去才作用到它
            reason = "免费技能，买的随从打出去后再用"
        else:
            reason = "免费技能，每回合都用掉"
        self.actions.append(Action(type="hero_power", reason=reason))

    def maybe_refresh(self) -> None:
        cost = self.ctx.refresh_cost
        if cost is None or self.gold < cost:
            return
        has_pair = any(n == 2 for n in self.copies.values())
        goal = "找三连" if has_pair else "找更好的牌"
        self.actions.append(Action(type="refresh", reason=f"剩 {self.gold} 金，刷新{goal}"))
        self.gold -= cost


def _buy_summary(actions: list[Action]) -> str:
    """按方案里实际有的动作写总结。"""
    types = {a.type for a in actions}
    if "buy" in types:
        return "买最好的身材"
    parts = []
    if "hero_power" in types:
        parts.append("用掉免费的英雄技能")
    if "refresh" in types:
        parts.append("刷新找牌")
    return "，".join(parts) or "本回合不需要操作"


def rule_plans(ctx: TurnContext, cfg: RulesConfig, name: Namer) -> list[Plan]:
    """返回最多 3 个合法方案，最优的在前。"""
    direction = tribe_zh(ctx.main_tribe) if ctx.main_tribe else None
    drafts: list[tuple[str, _Draft]] = []

    triple = next(
        (
            i
            for i in ctx.shop
            if i.card_type == MINION and _Draft(ctx, cfg, name).completes_triple(i)
        ),
        None,
    )
    if triple is not None:
        d = _Draft(ctx, cfg, name)
        d.buy(triple, f"凑成三连：{name(triple.card_id)}变金色，还能发现一张高一本的随从")
        d.fill_buys()
        d.maybe_hero_power()
        d.maybe_refresh()
        drafts.append(("三连优先", d))

    upgrade_draft = None
    target = cfg.target_tier(ctx.turn, ctx.effective_health)
    cost = ctx.upgrade_cost
    if cost is not None and ctx.tier < target and ctx.gold >= cost:
        d = _Draft(ctx, cfg, name)
        if cfg.target_tier(ctx.turn, 0) > ctx.tier:
            reason = f"第 {ctx.turn} 回合按节奏该升 {ctx.tier + 1} 本了"
        else:
            reason = f"血量健康，第 {ctx.turn} 回合可以提前升 {ctx.tier + 1} 本"
        d.upgrade(reason)
        d.fill_buys()
        d.maybe_hero_power()
        d.maybe_refresh()
        upgrade_draft = ("按节奏升本", d)

    buy = _Draft(ctx, cfg, name)
    buy.fill_buys()
    buy.maybe_hero_power()
    buy.maybe_refresh()
    buy_draft = (_buy_summary(buy.actions), buy)

    if upgrade_draft is not None and ctx.effective_health > cfg.safe_below:
        drafts += [upgrade_draft, buy_draft]
    else:
        drafts.append(buy_draft)
        if upgrade_draft is not None:
            drafts.append(upgrade_draft)

    plans: list[Plan] = []
    seen: set[tuple] = set()
    for summary, d in drafts:
        plan = Plan(source="rules", actions=tuple(d.actions), summary=summary, direction=direction)
        if plan.signature() in seen or validate_plan(plan.actions, ctx) is not None:
            continue
        seen.add(plan.signature())
        plans.append(plan)
    return plans[:MAX_PLANS]
