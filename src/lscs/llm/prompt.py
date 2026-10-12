"""生成发给大模型的系统提示和局面消息（M2 设计文档第 7.1 节、更聪明的 AI 建议设计文档第 3 节）。
选英雄（选英雄建议设计文档第 5 节）和游戏弹出的"选一个"（选项建议设计文档第 5 节）各用单独的
系统提示和局面消息，招募阶段的提示不受影响。"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from lscs.advisor.context import MINION, TurnContext, hero_power_blocker, hero_power_used
from lscs.advisor.models import Lane
from lscs.advisor.names import armor_zh, choice_title, keyword_zh, minion_label, tribe_zh
from lscs.advisor.opponents import (
    OpponentSummary,
    contested_parts,
    eliminated_names,
    summarize_opponents,
)
from lscs.advisor.pool import PoolNote, in_lobby, pool_notes
from lscs.cards.db import CardDB, CardInfo
from lscs.llm.parse import MAX_ACTIONS
from lscs.state.models import (
    NO_TARGET,
    ChoiceKind,
    ChoiceOption,
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
    "summary": "本回合目标，25 字以内，例如：补两张畸变铺场，下回合再升 4 本",
    "direction": "方向/流派",
    "look_for": ["接下来要找的牌名"],
    "confidence": 0.7,
}

# 更聪明的 AI 建议设计文档第 3.1 节的原文（实验验证过），不要随意改动
SYSTEM_TEMPLATE = """你是《炉石传说》酒馆战棋的高分教练，正在给一位 6000 分以下的新手实时指挥招募阶段的操作。目标是稳定进前四、争取第一。请用简体中文，具体、直接。

【先分析，再给操作】
先在 analysis 里用两三句话写清：现在是什么阶段、血量和场面强不强、经济怎么样；本局准备走什么方向（种族/流派）以及理由；本回合的目标（升本？补战力？找对子三连？找核心卡？）。然后再按这个目标给操作。
summary 用一句话（25 字以内）直接写本回合要做什么、为了什么，它会显示在游戏画面上的小面板里，所以要短、要完整，不要半句话。

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

# 选英雄建议设计文档第 5.1 节的原文。招募阶段的 SYSTEM_TEMPLATE 一个字都不改（DeepSeek 按前缀
# 缓存）；选英雄每局只调一次，缓存不命中无所谓
HERO_EXAMPLE = {
    "pick": 89,
    "reason": "为什么选它，30 字以内，会显示在游戏画面上",
    "heroes": [
        {"entity_id": 90, "comment": "一句点评，15 字以内"},
        {"entity_id": 91, "comment": "..."},
    ],
    "confidence": 0.7,
}
HERO_SYSTEM_TEMPLATE = """你是《炉石传说》酒馆战棋的高分教练，正在帮一位 6000 分以下的新手在开局的 4 个备选英雄里选一个。只有大约 1 分钟，请用简体中文，直接给结论。

【怎么判断】
- 护甲：官方用起始护甲做平衡——越强的英雄给的护甲越少，护甲最少的英雄通常就是当前版本最强的。这是最可靠的强度信号，默认按它选；只有技能明显不适合新手、或明显依赖本局没有的条件时才偏离。
- 技能：看技能描述。新手优先选"每回合都有稳定收益、不需要复杂操作"的技能（加属性、给铸币、减费、免费刷新），少选要靠特定种族、特定卡或精细计算才强的技能。
- 本局种族在选英雄时还看不到，所以依赖某个种族的技能要打折扣；只有本局机制（如神明）是已知的，可以结合它判断。
- 不知道的事不要编：没有给出的数据（如胜率榜）不要假装知道。

只输出一个 json 对象，格式：
{example}
pick 必须是备选英雄的实体编号；heroes 给其他每个英雄一句点评（推荐的那个也可以给）。

【攻略要点】
{guide}

【本月英雄强度资料】
{heroes_note}"""  # noqa: E501
NO_HEROES_NOTE = "（本月还没有可靠的英雄强度资料，请按护甲和技能判断。）"

# 选项建议设计文档第 5.1 节的原文。选项在一局里会弹出很多次，系统提示本身不变，也能命中缓存
CHOICE_EXAMPLE = {"pick": 6933, "reason": "为什么选它，25 字以内", "confidence": 0.7}
CHOICE_SYSTEM_TEMPLATE = """你是《炉石传说》酒馆战棋的高分教练，正在帮一位 6000 分以下的新手在游戏弹出的几个选项里选一个（三连奖励的发现、黑暗之赐、饰品、英雄技能的升级选项，或者随从效果的发现）。玩家几秒钟内就要点，请用简体中文，直接给结论。

【怎么判断】
- 先看本局方向和场面：和当前方向（种族/流派）、场上核心配合的选项优先；还没有方向时选单独最强、最稳、新手最容易用好的。
- 三连奖励 / 发现随从：优先方向核心卡、能成长的牌、有经济价值的牌，其次才看身材；能和手里、场上凑成对子或三连的也值得。
- 黑暗之赐：每个选项的属性已经算上了赐福。比较"随从本身 + 赐福"对现在阵容的价值；圣盾、复生、风怒这类关键词放在主力随从身上最值。
- 饰品：看它在接下来几个回合能带来多少收益；和方向配合、现在就能用上的优先；要花铸币的饰品要考虑这回合剩多少铸币、会不会耽误升本。
- 英雄技能的升级选项：选和自己的阵容、打法最契合的；几个选项差不多时选收益最稳定的。
- 不知道的事不要编：没有给出的数据不要假装知道。

只输出一个 json 对象，格式：
{example}
pick 必须是选项的实体编号；reason 会显示在游戏画面上，25 字以内。

【攻略要点】
{guide}"""  # noqa: E501

# AI 的回答不合法时的改正要求（AI 自我改正设计文档第 3.3 节）：作为新的一轮 user 消息，接在原局面
# 消息和模型自己的回答之后。系统提示不动（前缀缓存）；DeepSeek 的 json 模式要求提示里有"json"
REPAIR_PLAN = (
    "你刚才的方案不合法：{problem}。\n"
    "请按同样的规则改正后，重新输出完整的 json：analysis、summary、direction、look_for 保持不变，"
    "只改 actions；每一步都重新核对 gold_after 和 minions_after。"
)
REPAIR_PICK = "你刚才的回答不合法：{problem}。请按同样的规则改正后，重新输出完整的 json。"

MAX_TAVERN_TIER = 6  # 酒馆最高 6 本；7 本的牌不会出现在商店里，不让 AI 叫新手去找
POOL_TITLE = "【本局卡池（本局种族的随从和中立随从，按酒馆等级）】"
# 对手块（对手一览与卡池估计设计文档第 8 节）：自带说明，系统提示不改
OPPONENTS_TITLE = (
    "【对手】按名次；▶ 下一个对手；★ 最强威胁（0~100，按最近见到的阵容、酒馆等级和血量粗估）。"
    "阵容是最近一次交手时看到的，之后可能变强了。同一种族被多家主打时，这个种族的牌更难凑。"
)
NO_OPPONENTS = "（还没有对手的信息）"
POOL_LINE_TITLE = "卡池估计（还能刷到的张数）："
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


def build_hero_system_prompt(guide: str, heroes_note: str | None) -> str:
    """heroes_note：strategy/current/heroes.md 的全文（当月的英雄强度资料）；没有时为 None。"""
    return HERO_SYSTEM_TEMPLATE.format(
        example=json.dumps(HERO_EXAMPLE, ensure_ascii=False),
        guide=guide,
        heroes_note=heroes_note or NO_HEROES_NOTE,
    )


def build_choice_system_prompt(guide: str) -> str:
    return CHOICE_SYSTEM_TEMPLATE.format(
        example=json.dumps(CHOICE_EXAMPLE, ensure_ascii=False), guide=guide
    )


def repair_text(lane: Lane, problem: str) -> str:
    """problem：校验器或解析器给出的原因，原样发回去。招募的两路用 REPAIR_PLAN，
    选英雄、选项用 REPAIR_PICK。"""
    template = REPAIR_PLAN if lane in ("fast", "deep") else REPAIR_PICK
    return template.format(problem=problem)


def _in_tavern(card: CardInfo) -> bool:
    return (card.tier or 0) <= MAX_TAVERN_TIER


def _races_zh(card: CardInfo) -> str:
    return "、".join(tribe_zh(r) for r in card.races) or "中立"


def card_pool_block(cards: CardDB, tribes: frozenset[str]) -> str:
    """本局卡池：放在局面消息开头，和系统提示一起组成稳定的前缀（只在本局种族变化时才变）。
    种族还在推断中时用当前推断出的种族。不列 7 本的随从和法术。"""
    minions = sorted(
        (c for c in cards.pool_minions() if _in_tavern(c) and in_lobby(c, tribes)),
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

    def minion_parts(self, card: Minion | ShopItem | HandCard | ChoiceOption) -> list[str]:
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


def _mechanics_line(state: GameState, cards: CardDB) -> str | None:
    """本局机制（古神等），招募和选英雄的消息共用；卡牌数据里都查不到时为 None。"""
    mechanics = [cards.get_by_dbf(dbf) for dbf in state.global_dbfs]
    if not any(mechanics):
        return None
    return "本局机制：" + "；".join(f"{m.name}（{m.text}）" for m in mechanics if m is not None)


def _turn_line(state: GameState, w: _Writer) -> str:
    me = state.me
    assert me is not None
    return (
        f"第 {state.turn} 回合（招募）| 英雄：{w.name(me.hero_card_id)} "
        f"| 血量 {me.health}+{me.armor} | 铸币 {me.gold} | 酒馆 {me.tavern_tier} 本"
    )


def _tribes_line(state: GameState) -> str:
    tribes = "、".join(tribe_zh(t) for t in sorted(state.tribes)) or "未知"
    if state.tribes_confidence == TribesConfidence.INFERRING:
        tribes += "（推断中）"
    return f"本局种族：{tribes}"


def _trinkets_line(trinkets: Sequence[str], w: _Writer) -> str:
    return "饰品：" + "；".join(f"{w.name(t)}（{w.text(t)}）" for t in trinkets)


def _seen_text(s: OpponentSummary, board: Sequence[Minion], full: bool, w: _Writer) -> str:
    """见过的阵容：几个随从、主种族、总身材、金色；full 时列完整阵容（带身材），否则只列主力。"""
    if s.empty_board:
        return f"第 {s.seen_turn} 回合见到空场"
    parts = [f"第 {s.seen_turn} 回合见到 {s.minions} 个随从"]
    if s.main_tribe is not None:
        parts.append(f"{tribe_zh(s.main_tribe)}为主（{s.tribe_count} 个）")
    parts.append(f"总身材 {s.total_stats}")
    if s.golden:
        parts.append(f"金色 {s.golden} 张")
    if full and board:
        parts.append("阵容：" + "、".join(w.minion_parts(m)[0] for m in board))
    elif s.key_cards:
        parts.append("主力：" + "、".join(minion_label(m, w.name) for m in s.key_cards))
    return "，".join(parts)


def _opponent_line(s: OpponentSummary, state: GameState, w: _Writer) -> str:
    marks = ("▶" if s.next else "") + ("★" if s.strongest else "")
    prefix = f"{marks} " if len(marks) == 2 else f"{marks or ' '} "
    head = f"{prefix}第 {s.place if s.place is not None else '?'} 名 {w.name(s.hero_card_id)}"
    opp = state.opponents.get(s.player_id)
    board = opp.last_board if opp is not None else ()
    if not s.alive:
        if not s.next:
            return f"{head} | 已淘汰"
        if s.seen_turn is None:
            seen = "还没见过阵容"
        elif not board:
            seen = f"第 {s.seen_turn} 回合见到空场"
        else:
            cards = "、".join(w.minion_parts(m)[0] for m in board)
            seen = f"第 {s.seen_turn} 回合见到的阵容：{cards}"
        return f"{head} | 已淘汰，这回合打它的残影 | {seen}"
    tier = "?" if s.tavern_tier is None else s.tavern_tier
    seen = "还没见过阵容" if s.seen_turn is None else _seen_text(s, board, s.next, w)
    return f"{head} | 血量 {s.health}+{s.armor} | 酒馆 {tier} 本 | 威胁 {s.threat} | {seen}"


def build_opponents_block(state: GameState, cards: CardDB, all_opponents: bool = True) -> str:
    """对手块：放在卡池块之后、回合内容之前。阵容只在战斗之后变；对手的酒馆等级和血量在招募
    中途也会变（升本、加护甲），所以调度器每回合第一次生成后整回合复用它，同一回合内不变，
    能多命中一段前缀缓存。all_opponents=False 时只写说明行和下一个对手。"""
    w = _Writer(cards)
    overview = summarize_opponents(state)
    lines = [OPPONENTS_TITLE]
    if all_opponents and overview.opponents:
        parts = contested_parts(overview)
        if overview.unknown:
            parts.append(f"未知 {overview.unknown} 家")
        lines.append("主打种族：" + ("、".join(parts) or "看不出"))
    shown = [s for s in overview.opponents if s.next or (all_opponents and s.alive)]
    lines += [_opponent_line(s, state, w) for s in shown]
    gone = eliminated_names(overview, w.name)
    if all_opponents and gone:
        lines.append("  已淘汰：" + "、".join(gone))  # 残影以外的淘汰玩家合成一行
    if len(lines) == 1:
        lines.append(NO_OPPONENTS)
    return "\n".join(lines)


def _pool_note_text(note: PoolNote) -> str:
    owned = []
    if note.have:
        owned.append(f"你有 {note.have} 张")
    if note.shop:
        owned.append(f"商店里有 {note.shop} 张")
    tail = f"（{'，'.join(owned)}）" if owned else ""
    return f"{note.name} 还剩约 {note.left} 张{tail}"


def _pool_line(
    state: GameState, ctx: TurnContext, memory: AIMemory | None, w: _Writer
) -> str | None:
    """对子、差一张成对子和记忆里要找的牌还剩约几张；没有卡池估计或没有这些牌时为 None。"""
    if ctx.pool is None:
        return None
    shop = [s.card_id for s in state.shop if s.card_type == MINION]
    look_for = memory.look_for if memory is not None else ()
    notes = pool_notes(ctx.pool, ctx, shop, look_for, w.name)
    if not notes:
        return None
    return POOL_LINE_TITLE + "；".join(_pool_note_text(n) for n in notes)


def build_user_message(
    state: GameState,
    ctx: TurnContext,
    cards: CardDB,
    memory: AIMemory | None = None,
    all_opponents: bool = True,
    opponents_block: str | None = None,
) -> str:
    """本局卡池 + 空行 + 对手 + 空行 + 局面；有记忆时末尾加一行之前的判断。不附规则建议
    （AI 曾经照抄）。卡池块和对手块在前：跨回合的缓存前缀（系统提示 + 卡池块）不变，
    对手块同一回合内不变。opponents_block：调度器这一回合已经生成的对手块；
    None 时按 state 现生成。"""
    me = state.me
    assert me is not None
    w = _Writer(cards)
    lines = [
        _turn_line(state, w),
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
    lines.append(_tribes_line(state))
    if me.trinkets:
        lines.append(_trinkets_line(me.trinkets, w))
    mechanics = _mechanics_line(state, cards)
    if mechanics is not None:
        lines.append(mechanics)
    lines += w.section("商店：", state.shop)
    lines += w.section(f"场面（已用 {len(me.board)}/7）：", me.board)
    lines += w.section("手牌：", me.hand)
    pool = _pool_line(state, ctx, memory, w)
    if pool is not None:
        lines.append(pool)
    if memory is not None:
        lines.append(_memory_line(memory, state.turn))
    return "\n\n".join(
        (
            card_pool_block(cards, state.tribes),
            opponents_block
            if opponents_block is not None
            else build_opponents_block(state, cards, all_opponents),
            "\n".join(lines),
        )
    )


def _hero_power_part(dbf: int | None, cards: CardDB) -> str:
    power = cards.get_by_dbf(dbf) if dbf is not None else None
    if power is None:
        return "技能 未知"
    cost = "?" if power.cost is None else power.cost
    text = f"：{power.text}" if power.text else ""  # 被动技能的描述本身就写着"被动"
    return f"技能 {power.name}（{cost} 金）{text}"


def build_hero_message(state: GameState, cards: CardDB, rerolled: bool = False) -> str:
    """选英雄的局面消息：本局机制、备选英雄的护甲和技能。不放本局卡池（种族还不知道）。
    rerolled：玩家重新随机过英雄。"""
    select = state.hero_select
    assert select is not None
    w = _Writer(cards)
    count = len(select.offered)
    lines = [f"选英雄（{count} 选 1，已重新随机）" if rerolled else f"选英雄（{count} 选 1）"]
    mechanics = _mechanics_line(state, cards)
    if mechanics is not None:
        lines.append(mechanics)
    lines += ["本局种族：未知（选英雄时还看不到）", "备选英雄："]
    lines += [
        f"  [#{h.entity_id}] {w.name(h.card_id)} | {armor_zh(h.armor)} | "
        + _hero_power_part(h.hero_power_dbf, cards)
        for h in select.offered
    ]
    return "\n".join(lines)


def _choice_heading(state: GameState, w: _Writer) -> str:
    choice = state.choice
    assert choice is not None
    count = f"{len(choice.options)} 选 1"
    title = choice_title(choice, w.name)
    if choice.kind == ChoiceKind.HERO_POWER:
        return f"选择：{title}的升级选项（{count}）"
    if choice.kind == ChoiceKind.DISCOVER:
        return f"选择：发现（{count}，来源：{title}）"
    return f"选择：{title}（{count}）"


def _gift_part(gifts: Sequence[str], w: _Writer) -> str:
    parts = [f"{w.name(g)}（{w.text(g)}）" if w.text(g) else w.name(g) for g in gifts]
    return "黑暗之赐：" + "；".join(parts)


def _choice_option_line(option: ChoiceOption, w: _Writer) -> str:
    """选项一行；其中"左 N"和面板、画面高亮用的是同一个序号。"""
    head = f"  [#{option.entity_id}] 左 {option.index + 1} "
    text = w.text(option.card_id)
    if option.card_type == "MINION" and option.atk is not None:
        parts = w.minion_parts(option)
        if text:
            parts.append(text)
        if option.gifts:
            parts.append(_gift_part(option.gifts, w))
        return head + " | ".join(parts)
    name = w.name(option.card_id)
    if option.card_type == "BATTLEGROUND_TRINKET" and option.cost is not None:
        name += f"（{option.cost} 金）"
    return head + (f"{name}：{text}" if text else name)


def build_choice_message(state: GameState, cards: CardDB, memory: AIMemory | None = None) -> str:
    """游戏弹出的"选一个"：标题、回合、本局种族和机制、场面、手牌和选项。不放本局卡池
    （选项就那么几张，发卡池只会更慢）；有记忆时末尾加一行之前的判断（只读，不会被改写）。"""
    choice, me = state.choice, state.me
    assert choice is not None and me is not None
    w = _Writer(cards)
    lines = [_choice_heading(state, w), _turn_line(state, w), _tribes_line(state)]
    mechanics = _mechanics_line(state, cards)
    if mechanics is not None:
        lines.append(mechanics)
    if choice.kind == ChoiceKind.HERO_POWER:
        power = choice.source_card_id
        text = w.text(power)
        lines.append(f"英雄技能：{w.name(power)}：{text}" if text else f"英雄技能：{w.name(power)}")
    if me.trinkets:
        lines.append(_trinkets_line(me.trinkets, w))
    lines += w.section(f"场面（已用 {len(me.board)}/7）：", me.board)
    lines += w.section("手牌：", me.hand)
    lines.append("选项：")
    lines += [_choice_option_line(option, w) for option in choice.options]
    if choice.kind == ChoiceKind.TRINKET:
        lines.append(f"铸币 {me.gold}；饰品的铸币价会从这回合的铸币里扣")
    if memory is not None:
        lines.append(_memory_line(memory, state.turn))
    return "\n".join(lines)
