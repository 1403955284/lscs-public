"""从 GameState 推出规则和校验需要的事实。"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from lscs.state.models import (
    ALREADY_USED,
    NO_TARGET,
    NOT_ENOUGH_GOLD,
    GameState,
    HandCard,
    HeroPower,
    Minion,
    Phase,
    ShopItem,
)
from lscs.state.pool import GOLDEN_SUFFIX, base_card_id
from lscs.state.tribes import IGNORED_RACES

SPELL = "BATTLEGROUND_SPELL"
MINION = "MINION"
MAX_BOARD = 7


@dataclass(frozen=True)
class TurnContext:
    turn: int
    gold: int
    tier: int
    effective_health: int  # 血量 + 护甲
    buy_cost: int
    upgrade_cost: int | None
    refresh_cost: int | None
    shop: tuple[ShopItem, ...]
    board: tuple[Minion, ...]
    hand: tuple[HandCard, ...]
    copies: Mapping[str, int]  # 普通卡 ID -> 场面 + 手牌里的非金色张数
    main_tribe: str | None
    hero_power: HeroPower | None = None

    @property
    def board_minions(self) -> int:
        return len(self.board)

    @property
    def hand_minions(self) -> int:
        return sum(1 for h in self.hand if h.card_type == MINION)

    @property
    def minion_limit(self) -> int:
        """方案执行后最多允许的随从数（假设手牌里的随从都会打出）。
        开局就超过 7 个时（场面满了手里还有随从），以开局数量为上限：方案不能让情况更糟。"""
        return max(MAX_BOARD, self.board_minions + self.hand_minions)

    def price(self, item: ShopItem) -> int:
        """酒馆法术按自己的 COST 收费；随从按买牌价。"""
        if item.card_type == SPELL and item.cost is not None:
            return item.cost
        return self.buy_cost


def hero_power_used(hp: HeroPower) -> bool:
    """本回合已经用过：EXHAUSTED，或者选项说 REQ_NOT_EXHAUSTED_HERO_POWER。"""
    return hp.used_this_turn or hp.unusable_reason == ALREADY_USED


def hero_power_blocker(hp: HeroPower | None) -> str | None:
    """英雄技能现在为什么不能用；能用（或者只差铸币）返回 None。铸币由调用方按模拟的铸币判断。"""
    if hp is None:
        return "没有英雄技能"
    if hero_power_used(hp):
        return "本回合已经用过英雄技能"
    if hp.option == "absent":
        return "英雄技能是被动的，不能主动使用" if hp.passive else "英雄技能现在不能用"
    if hp.option == "unknown":
        return "还不知道英雄技能现在能不能用"
    if hp.option == "unusable" and hp.unusable_reason != NOT_ENOUGH_GOLD:
        if hp.unusable_reason == NO_TARGET:
            return "英雄技能现在没有可选的目标"
        return f"英雄技能现在不能用（{hp.unusable_reason}）"
    return None


def is_golden(card_id: str, golden: bool) -> bool:
    return golden or card_id.endswith(GOLDEN_SUFFIX)


def _main_tribe(minions: Iterable[Minion | HandCard], lobby: frozenset[str]) -> str | None:
    counts: Counter[str] = Counter()
    stats: Counter[str] = Counter()
    for m in minions:
        for race in m.races:
            if race in IGNORED_RACES or (lobby and race not in lobby):
                continue
            counts[race] += 1
            stats[race] += (m.atk or 0) + (m.health or 0)
    if not counts:
        return None
    # 数量最多；数量相同时取总身材更高的；再相同时按名字，保证结果确定
    return max(counts, key=lambda r: (counts[r], stats[r], r))


def build_context(state: GameState, buy_cost: int) -> TurnContext | None:
    """只在招募阶段有意义；其他阶段返回 None。"""
    if state.phase != Phase.RECRUIT or state.me is None:
        return None
    me = state.me
    minions: list[Minion | HandCard] = list(me.board)
    minions.extend(h for h in me.hand if h.card_type == MINION)
    copies = Counter(base_card_id(m.card_id) for m in minions if not is_golden(m.card_id, m.golden))
    return TurnContext(
        turn=state.turn,
        gold=me.gold,
        tier=me.tavern_tier,
        effective_health=me.health + me.armor,
        buy_cost=buy_cost,
        upgrade_cost=me.upgrade_cost,
        refresh_cost=me.refresh_cost,
        shop=state.shop,
        board=me.board,
        hand=me.hand,
        copies=dict(copies),
        main_tribe=_main_tribe(minions, state.tribes),
        hero_power=me.hero_power,
    )
