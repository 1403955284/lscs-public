"""生成发给大模型的系统提示和局面消息（M2 设计文档第 7.1 节、更聪明的 AI 建议设计文档第 3 节）。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lscs.advisor.context import MINION, TurnContext, hero_power_blocker, hero_power_used
from lscs.advisor.names import keyword_zh, tribe_zh
from lscs.cards.db import CardDB, CardInfo
from lscs.llm.parse import MAX_ACTIONS
from lscs.state.models import (
    NO_TARGET,
    GameState,
    HandCard,
    HeroPower,
    Minion,
    ShopItem,
    TribesConfidence,
)

# 系统提示里的 JSON 示例（快速和深度共用同一个系统提示，方便 DeepSeek 缓存前缀）
EXAMPLE = {
    "analysis": "局势判断、本局方向、本回合目标（两三句）",
    "actions": [
        {
            "type": "buy",
            "entity_id": 1689,
            "gold_after": 4,
            "minions_after": 5,
            "reason": "一句话理由",
        },
        {"type": "refresh", "gold_after": 3, "minions_after": 5, "reason": "..."},
    ],
    "summary": "一句话说明本回合思路",
    "direction": "方向/流派",
    "look_for": ["接下来要找的牌名"],
    "confidence": 0.7,
}

# 更聪明的 AI 建议设计文档第 3.1 节的原文（实验验证过），不要随意改动
SYSTEM_TEMPLATE = """你是《炉石传说》酒馆战棋的高分教练，正在给一位 6000 分以下的新手实时指挥招募阶段的操作。目标是稳定进前四、争取第一。请用简体中文，具体、直接。

【先分析，再给操作】
先在 analysis 里用两三句话写清：现在是什么阶段、血量和场面强不强、经济怎么样；本局准备走什么方向（种族/流派）以及理由；本回合的目标（升本？补战力？找对子三连？找核心卡？）。然后再按这个目标给操作。

【运营方法】
- 阶段：
  - 前期（1~3 本，大约第 1~5 回合）：按节奏升本；剩下的铸币买当前最能打、或者有成长/配合/经济价值的随从。前期身材重要，但同样身材时优先带圣盾、复生、能成长或能给钱的牌。
  - 中期（4 本，大约第 6~8 回合）：根据场面、商店和本局种族定下一个主方向，开始找这个方向的核心卡；手里有对子就值得刷新找第三张；三连发现的牌优先选方向核心。
  - 后期（5~6 本）：围绕核心卡成型；铸币主要用来刷新找核心卡、三连，以及把方向外的过渡白板换成方向内的牌。
- 牌的价值不只是身材：能成长（每回合/每次触发变大）、和场上种族或机制配合、提供经济（给铸币、免费刷新、减费）、带关键词（圣盾、复生、剧毒、风怒）的牌，往往比一张身材大但没效果的白板更值钱。酒馆法术（加属性、给铸币）也是资源。
- 刷新：**刷新后程序会立刻把新商店发给你，你会再给一次建议，所以可以放心建议刷新。** 这些情况应该刷新：该买的买完了还有铸币；商店里没有对方向有用的牌；在找三连的第三张或核心卡。不要为了凑合而买一张对阵容没帮助的白板。
- 升本：参考攻略里的节奏；血量健康、场面不差时可以提前升本；连续掉血、场面明显偏弱时先补战力。升本的回合，剩下的铸币再买牌或刷新。
- 冻结：只在商店里有核心卡/三连需要的牌、但这回合买不起时冻结。
- 卖牌：场面满了要买关键牌时，卖掉对方向没用、身材最小的过渡牌（卖出 +1 金）。被养大的随从、金色随从和方向核心不要卖。
- 三连：凑成三连会拿到金色随从并发现一张高一本的牌，是重要的强度来源。

【必须遵守的规则】
1. 只能使用局面中给出的实体编号（#数字）：buy 只能买"商店"里的，sell 只能卖"场面"上的。
2. 每个动作都要填 gold_after（执行完这一步后剩多少铸币，不能小于 0）和 minions_after（场面 + 手牌随从一共几个，买 +1、卖 -1、凑成三连 -2），任何一步都不能超过随从上限。
3. 最多 {max_actions} 个动作；refresh 要放在最后（之后看不到新商店，所以 refresh 后面不要再冻结）；freeze 只能放在最后。
4. 英雄技能：局面里写了"不要安排 hero_power"时绝对不要安排；可以用时考虑使用时机（买来的随从在手牌里，要先打出去，给场上随从加属性的技能才会作用到它）；需要目标时 entity_id 填目标编号。
5. 不要建议发现选择、黑暗之赐或调整站位。

只输出一个 json 对象，格式：
{example}
动作类型：buy（买）、sell（卖）、upgrade（升本）、refresh（刷新）、freeze（冻结）、hero_power（英雄技能）。buy 和 sell 必须带 entity_id。

【攻略要点】
{guide}"""  # noqa: E501

MAX_TAVERN_TIER = 6  # 酒馆最高 6 本；7 本的牌不会出现在商店里，不让 AI 叫新手去找
POOL_TITLE = "【本局卡池（本局种族的随从和中立随从，按酒馆等级）】"
SPELLS_TITLE = "【酒馆法术】"


@dataclass(frozen=True)
class AIMemory:
    """本局最近一次合法 AI 结果里的判断，下次调用时附在局面消息末尾（跨回合记忆）。"""

    turn: int
    deep: bool  # 是否来自深度分析
    direction: str | None = None
    look_for: tuple[str, ...] = ()
    analysis: str = ""


def load_guide(path: Path) -> str:
    if not path.exists():
        return "（无）"
    # 记事本可能带 BOM，或者存成 GBK：不能因此启动失败，读不懂的字节换成替换字符
    return path.read_text(encoding="utf-8-sig", errors="replace").strip()


def build_system_prompt(guide: str) -> str:
    return SYSTEM_TEMPLATE.format(
        example=json.dumps(EXAMPLE, ensure_ascii=False), guide=guide, max_actions=MAX_ACTIONS
    )


def _in_tavern(card: CardInfo) -> bool:
    return (card.tier or 0) <= MAX_TAVERN_TIER


def _in_lobby(card: CardInfo, tribes: frozenset[str]) -> bool:
    """本局种族的随从、中立随从（没有种族）和全种族随从。"""
    return not card.races or "ALL" in card.races or bool(tribes.intersection(card.races))


def _races_zh(card: CardInfo) -> str:
    return "、".join(tribe_zh(r) for r in card.races) or "中立"


def card_pool_block(cards: CardDB, tribes: frozenset[str]) -> str:
    """本局卡池：放在局面消息开头，和系统提示一起组成稳定的前缀（只在本局种族变化时才变）。
    种族还在推断中时用当前推断出的种族。不列 7 本的随从和法术。"""
    minions = sorted(
        (c for c in cards.pool_minions() if _in_tavern(c) and _in_lobby(c, tribes)),
        key=lambda c: (c.tier or 0, _races_zh(c), c.name),
    )
    lines = [POOL_TITLE]
    lines += [
        f"[{c.tier or '?'}本] {c.name} {c.atk}/{c.health}（{_races_zh(c)}）{c.text}"
        for c in minions
    ] or ["（无）"]
    lines.append(SPELLS_TITLE)
    spells = sorted(filter(_in_tavern, cards.pool_spells()), key=lambda c: (c.tier or 0, c.name))
    lines += [
        f"[{c.tier or '?'}本 法术 {'?' if c.cost is None else c.cost}金] {c.name}"
        + (f"：{c.text}" if c.text else "")
        for c in spells
    ] or ["（无）"]
    return "\n".join(lines)


def _memory_line(memory: AIMemory, turn: int) -> str:
    parts = [
        f"方向 {memory.direction or '未定'}",
        f"要找：{'、'.join(memory.look_for) or '无'}",
    ]
    if memory.analysis:
        parts.append("分析：" + " ".join(memory.analysis.split()))
    body = "；".join(parts)
    if memory.deep and memory.turn == turn:
        return f"本回合深度分析：{body}"
    return f"你之前的判断（第 {memory.turn} 回合）：{body}"


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


def _entity_names(state: GameState) -> dict[int, str]:
    """局面里看得到的实体编号 -> 卡牌 ID：商店、场面、手牌。"""
    names = {s.entity_id: s.card_id for s in state.shop}
    if state.me is not None:
        names.update({m.entity_id: m.card_id for m in state.me.board})
        names.update({h.entity_id: h.card_id for h in state.me.hand})
    return names


DO_NOT_USE = "——不要安排 hero_power"


def _blocked_status(hp: HeroPower) -> str:
    """不能用的技能写明原因，并明确说不要安排（AI 曾在"本回合已使用"时照样安排）。"""
    if hero_power_used(hp):
        what = "本回合已使用，不能再用"
    elif hp.option == "absent" and hp.passive:
        what = "被动，不能主动使用"
    elif hp.option == "unknown":
        what = "还不确定本回合能不能用"
    elif hp.unusable_reason == NO_TARGET:
        what = "现在不能用：没有可选的目标"
    else:
        what = "现在不能用"
    return what + DO_NOT_USE


def _power_status(hp: HeroPower, state: GameState, w: _Writer) -> str:
    if hero_power_blocker(hp) is not None:
        return _blocked_status(hp)
    if hp.option == "usable":
        ready = "本回合可用"
        if hp.needs_target is False:
            aim = "不需要目标"
        else:
            names = _entity_names(state)
            targets = "、".join(
                f"#{t} {w.name(names[t])}" if t in names else f"#{t}" for t in hp.targets
            )
            aim = f"需要目标：{targets}"
    else:  # 只差铸币：这时选项不列目标，只能靠以前能用时记下的
        ready = "现在铸币不够，凑够铸币后可用"
        aim = {
            True: "需要目标；凑够铸币后在场上的随从里选",
            False: "不需要目标",
            # 校验器这时要求目标是场上的随从，告诉 AI 照这样填
            None: "是否需要目标未知；要用的话 entity_id 填场上的一个随从",
        }[hp.needs_target]
    return f"{hp.cost} 金，{ready}，{aim}"


def _hero_power_line(hp: HeroPower, state: GameState, w: _Writer) -> str:
    text = w.text(hp.card_id)
    line = f"英雄技能：{w.name(hp.card_id)}（{_power_status(hp, state, w)}）"
    return f"{line}：{text}" if text else line


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
    hp = ctx.hero_power
    if hp is not None:
        if hero_power_used(hp):
            line += "；英雄技能本回合已用过，不能再用"
        elif hp.option == "absent" and hp.passive:
            line += "；英雄技能是被动的"
        elif hero_power_blocker(hp) is not None:
            line += "；英雄技能现在不能用"
        elif hp.cost > 0:
            line += f"；英雄技能 {hp.cost} 金"
    return line


def build_user_message(
    state: GameState, ctx: TurnContext, cards: CardDB, memory: AIMemory | None = None
) -> str:
    """本局卡池 + 空行 + 局面；有记忆时末尾加一行之前的判断。不附规则建议（AI 曾经照抄）。"""
    me = state.me
    assert me is not None
    w = _Writer(cards)
    lines = [
        f"第 {state.turn} 回合（招募）| 英雄：{w.name(me.hero_card_id)} "
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
    if me.hero_power is not None:
        lines.append(_hero_power_line(me.hero_power, state, w))
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
    if memory is not None:
        lines.append(_memory_line(memory, state.turn))
    return card_pool_block(cards, state.tribes) + "\n\n" + "\n".join(lines)
