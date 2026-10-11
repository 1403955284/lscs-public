"""生成发给大模型的系统提示和局面消息（M2 设计文档第 7.1 节）。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from lscs.advisor.context import MINION, TurnContext
from lscs.advisor.models import Plan
from lscs.advisor.names import keyword_zh, tribe_zh
from lscs.cards.db import CardDB
from lscs.state.models import GameState, HandCard, Minion, ShopItem, TribesConfidence

LETTERS = "ABCDEFGH"
EXAMPLE = {
    "actions": [
        {
            "type": "buy",
            "entity_id": 1689,
            "gold_after": 4,
            "minions_after": 5,
            "reason": "凑成海盗对子",
        },
        {"type": "upgrade", "gold_after": 0, "minions_after": 5, "reason": "按节奏该升 3 本了"},
    ],
    "summary": "一句话说明本回合思路",
    "direction": "海盗",
    "confidence": 0.7,
}

SYSTEM_TEMPLATE = """你是《炉石传说》酒馆战棋的教练。
你在给一位 6000 分以下的新手玩家提供本回合招募阶段的操作建议。请用简体中文，简洁直接。

规则：
1. 只能使用局面中给出的实体编号（#数字）：buy 只能买"商店"里的，sell 只能卖"场面"上的。
2. 所有动作的总花费不能超过当前铸币：随从按"买牌"价格，酒馆法术按它标注的价格，卖出随从 +1 金。
3. 最多 4 个动作；freeze 只能放在最后。
   refresh 之后看不到新商店，所以 refresh 要放在最后，不要在 refresh 后面再冻结；
   freeze 只用来留住当前商店里看得到、想要但买不起的牌。
4. 场面最多 7 个随从，手牌里的随从也要算进去；三张相同的随从会合成一张金色。
5. 不要建议使用英雄技能、发现选择或调整站位。
6. 每个动作的 reason 用一句话说明原因。
7. 每个动作都要填 gold_after：执行完这一步后还剩多少铸币。先算清楚再写，任何一步都不能小于 0。
8. 每个动作都要填 minions_after：执行完这一步后场面随从 + 手牌随从一共几个
   （买随从 +1，卖 -1，凑成三连 -2）。任何一步都不能超过随从上限；满了要先卖再买。

只输出一个 json 对象，格式示例：
{example}

动作类型：buy（买）、sell（卖）、upgrade（升本）、refresh（刷新）、freeze（冻结）。
buy 和 sell 必须带 entity_id。
direction 填建议的方向或流派，没有就填空字符串；confidence 是 0 到 1 之间的把握程度。

以下是攻略要点，仅供参考：
{guide}
"""


def load_guide(path: Path) -> str:
    if not path.exists():
        return "（无）"
    # 记事本可能带 BOM，或者存成 GBK：不能因此启动失败，读不懂的字节换成替换字符
    return path.read_text(encoding="utf-8-sig", errors="replace").strip()


def build_system_prompt(guide: str) -> str:
    return SYSTEM_TEMPLATE.format(example=json.dumps(EXAMPLE, ensure_ascii=False), guide=guide)


class _Writer:
    def __init__(self, cards: CardDB) -> None:
        self.cards = cards

    def name(self, card_id: str | None) -> str:
        return self.cards.name(card_id) if card_id else "未知"

    def text(self, card_id: str | None) -> str:
        info = self.cards.get(card_id) if card_id else None
        return info.text if info is not None else ""

    def minion_parts(self, card: Minion | ShopItem | HandCard) -> list[str]:
        golden = "(金)" if card.golden and "(金)" not in self.name(card.card_id) else ""
        parts = [f"{self.name(card.card_id)}{golden} {card.atk}/{card.health}"]
        if card.tier:
            parts.append(f"{card.tier} 本")
        parts.append("、".join(tribe_zh(r) for r in card.races) or "中立")
        keywords = getattr(card, "keywords", ())
        if keywords:
            parts.append("、".join(keyword_zh(k) for k in keywords))
        return parts

    def card_line(self, card: Minion | ShopItem | HandCard) -> str:
        if getattr(card, "card_type", MINION) == MINION:
            parts = self.minion_parts(card)
        else:
            price = f"，{card.cost} 金" if getattr(card, "cost", None) is not None else ""
            parts = [f"{self.name(card.card_id)}（法术{price}）"]
        text = self.text(card.card_id)
        if text:
            parts.append(text)
        return f"  [#{card.entity_id}] " + " | ".join(parts)

    def section(self, title: str, cards: Sequence[Minion | ShopItem | HandCard]) -> list[str]:
        return [title] + ([self.card_line(c) for c in cards] or ["  （空）"])


def _plan_text(plan: Plan, state: GameState, w: _Writer) -> str:
    names = {s.entity_id: s.card_id for s in state.shop}
    if state.me is not None:
        names.update({m.entity_id: m.card_id for m in state.me.board})
    verbs = {"buy": "买", "sell": "卖", "upgrade": "升本", "refresh": "刷新", "freeze": "冻结"}
    steps = []
    for a in plan.actions:
        what = verbs[a.type]
        if a.entity_id is not None:
            what += f" #{a.entity_id} {w.name(names.get(a.entity_id))}"
        steps.append(f"{what}（{a.reason}）" if a.reason else what)
    return "；".join(steps) or "无需操作"


def _budget_line(ctx: TurnContext) -> str:
    """只用 TurnContext 里的数，和校验器（validate_plan）口径一致。"""
    used = ctx.board_minions + ctx.hand_minions
    room = max(ctx.minion_limit - used, 0)
    buys = ctx.gold // ctx.buy_cost if ctx.buy_cost > 0 else room  # 买牌价不会是 0，以防万一
    line = (
        f"预算：铸币 {ctx.gold}，只买随从最多 {buys} 张；随从上限 {ctx.minion_limit}，"
        f"现在 {used} 个（场面 {ctx.board_minions} + 手牌随从 {ctx.hand_minions}），"
        f"还能再买 {room} 个随从"
    )
    if ctx.upgrade_cost is None:
        line += "；已是最高等级，不能升本"
    return line


def build_user_message(
    state: GameState, ctx: TurnContext, cards: CardDB, rule_plans: Sequence[Plan]
) -> str:
    me = state.me
    assert me is not None
    w = _Writer(cards)
    hero_power = f"{w.name(me.hero_power_card_id)}：{w.text(me.hero_power_card_id)}"
    lines = [
        f"第 {state.turn} 回合（招募）| 英雄：{w.name(me.hero_card_id)}（技能 {hero_power}）"
        f"| 血量 {me.health}+{me.armor} | 铸币 {me.gold} | 酒馆 {me.tavern_tier} 本",
        " | ".join(
            [
                f"升本 {me.upgrade_cost} 金" if me.upgrade_cost is not None else "已是最高等级",
                f"刷新 {me.refresh_cost} 金" if me.refresh_cost is not None else "不能刷新",
                f"买牌 {ctx.buy_cost} 金",
                "卖牌 +1 金",
            ]
        ),
        _budget_line(ctx),
    ]
    tribes = "、".join(tribe_zh(t) for t in sorted(state.tribes)) or "未知"
    if state.tribes_confidence == TribesConfidence.INFERRING:
        tribes += "（推断中）"
    lines.append(f"本局种族：{tribes}")
    if me.trinkets:
        lines.append("饰品：" + "；".join(f"{w.name(t)}（{w.text(t)}）" for t in me.trinkets))
    mechanics = [cards.get_by_dbf(dbf) for dbf in state.global_dbfs]
    if any(mechanics):
        lines.append(
            "本局机制：" + "；".join(f"{m.name}（{m.text}）" for m in mechanics if m is not None)
        )
    lines += w.section("商店：", state.shop)
    lines += w.section(f"场面（已用 {len(me.board)}/7）：", me.board)
    lines += w.section("手牌：", me.hand)

    nxt = state.opponents.get(state.next_opponent_id) if state.next_opponent_id else None
    if nxt is not None:
        if nxt.last_board:
            board = "、".join(" ".join(w.minion_parts(m)[:1]) for m in nxt.last_board)
            seen = f"第 {nxt.last_seen_turn} 回合见到的阵容：{board}"
        else:
            seen = "还没见过阵容"
        lines.append(
            f"下一个对手：{w.name(nxt.hero_card_id)} 血量 {nxt.health}+{nxt.armor}；{seen}"
        )
    others = sorted(
        (o for o in state.opponents.values() if o is not nxt and o.health > 0),
        key=lambda o: (o.place or 99, o.player_id),
    )
    if others:
        lines.append(
            "其他对手："
            + "；".join(
                f"{w.name(o.hero_card_id)} 血量 {o.health}"
                + (f" 第 {o.place} 名" if o.place is not None else "")
                for o in others
            )
        )
    if rule_plans:
        lines.append("规则建议（仅供参考）：")
        lines += [
            f"  {LETTERS[i]}. {_plan_text(p, state, w)}" for i, p in enumerate(rule_plans[:3])
        ]
    return "\n".join(lines)
