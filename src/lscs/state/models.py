"""酒馆战棋状态快照和事件（不可变）。"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class Phase(StrEnum):
    WAITING = "WAITING"  # 没有对局，或者不是单人酒馆战棋
    HERO_SELECT = "HERO_SELECT"
    RECRUIT = "RECRUIT"
    COMBAT = "COMBAT"
    GAME_OVER = "GAME_OVER"


class TribesConfidence(StrEnum):
    CONFIRMED = "CONFIRMED"
    INFERRING = "INFERRING"


class EventType(StrEnum):
    GAME_STARTED = "GAME_STARTED"
    HERO_CHOSEN = "HERO_CHOSEN"
    RECRUIT_STARTED = "RECRUIT_STARTED"
    SHOP_CHANGED = "SHOP_CHANGED"
    BOARD_CHANGED = "BOARD_CHANGED"
    HAND_CHANGED = "HAND_CHANGED"
    GOLD_CHANGED = "GOLD_CHANGED"
    COMBAT_STARTED = "COMBAT_STARTED"
    COMBAT_ENDED = "COMBAT_ENDED"
    GAME_ENDED = "GAME_ENDED"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Minion(_Frozen):
    entity_id: int
    card_id: str
    position: int
    tier: int | None
    races: tuple[str, ...]
    atk: int
    health: int
    golden: bool
    keywords: tuple[str, ...]


class ShopItem(_Frozen):
    entity_id: int
    card_id: str
    card_type: str
    position: int
    tier: int | None
    races: tuple[str, ...]
    atk: int | None
    health: int | None
    golden: bool
    frozen: bool
    keywords: tuple[str, ...] = ()
    cost: int | None = None  # COST 标签：酒馆法术的购买价格；随从没有或不是价格


class HandCard(_Frozen):
    entity_id: int
    card_id: str
    card_type: str
    tier: int | None
    atk: int | None
    health: int | None
    golden: bool
    races: tuple[str, ...] = ()


class PlayerView(_Frozen):
    player_id: int | None
    hero_card_id: str | None
    hero_power_card_id: str | None
    health: int
    armor: int
    gold: int
    tavern_tier: int
    upgrade_cost: int | None
    refresh_cost: int | None
    board: tuple[Minion, ...]
    hand: tuple[HandCard, ...]
    trinkets: tuple[str, ...] = ()  # 我方饰品的卡牌 ID


class OpponentView(_Frozen):
    player_id: int
    hero_card_id: str | None
    health: int
    armor: int
    place: int | None
    tavern_tier: int | None
    last_board: tuple[Minion, ...] = ()
    last_seen_turn: int | None = None


class GameState(_Frozen):
    seq: int = 0
    phase: Phase = Phase.WAITING
    turn: int = 0  # 酒馆战棋回合数：(游戏 TURN + 1) // 2
    # 游戏实体的 STEP 是 MAIN_ACTION：招募阶段已经可以操作（铸币、商店、饰品选择都已就绪）
    main_action: bool = False
    me: PlayerView | None = None
    shop: tuple[ShopItem, ...] = ()
    opponents: dict[int, OpponentView] = {}
    next_opponent_id: int | None = None
    tribes: frozenset[str] = frozenset()
    tribes_confidence: TribesConfidence = TribesConfidence.INFERRING
    pool_estimate: dict[str, int] = {}
    uncertain: bool = False
    final_place: int | None = None
    global_dbfs: tuple[int, ...] = ()  # 本局全局机制（BACON_GLOBAL_*_DBID）的卡牌 dbfId


class Event(_Frozen):
    type: EventType
    turn: int
    data: dict[str, int | str | None] = {}
