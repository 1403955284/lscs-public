"""酒馆战棋状态快照和事件（不可变）。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

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
    # 选英雄阶段出现了一组新的备选英雄；data：count 个数，changed 本局之前已经有过一组（重随）
    HEROES_OFFERED = "HEROES_OFFERED"
    HERO_CHOSEN = "HERO_CHOSEN"
    RECRUIT_STARTED = "RECRUIT_STARTED"
    SHOP_CHANGED = "SHOP_CHANGED"
    BOARD_CHANGED = "BOARD_CHANGED"
    HAND_CHANGED = "HAND_CHANGED"
    GOLD_CHANGED = "GOLD_CHANGED"
    HERO_POWER_CHANGED = "HERO_POWER_CHANGED"
    COMBAT_STARTED = "COMBAT_STARTED"
    COMBAT_ENDED = "COMBAT_ENDED"
    GAME_ENDED = "GAME_ENDED"
    # 游戏弹出了一组新的"选一个"（GameState.choice）；data：id 编号，kind 种类，count 选项个数
    CHOICE_OFFERED = "CHOICE_OFFERED"
    # 那组选完了（或作废了）；data：id 编号，picked 选中的实体编号，card 它的卡牌 ID
    # （不知道选了哪个时 picked、card 都是 None）
    CHOICE_RESOLVED = "CHOICE_RESOLVED"


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


# 选项的错误码
NOT_ENOUGH_GOLD = "REQ_ENOUGH_MANA"
ALREADY_USED = "REQ_NOT_EXHAUSTED_HERO_POWER"
NO_TARGET = "REQ_TARGET_TO_PLAY"  # 需要目标，但现在没有可以选的目标


class HeroPower(_Frozen):
    """我方英雄技能。能不能用以本回合最近一组选项（GameState.DebugPrintOptions）为准。"""

    entity_id: int
    card_id: str
    cost: int  # COST 标签；日志不写值为 0 的标签，所以没有就是 0
    used_this_turn: bool  # EXHAUSTED：本回合已经用过
    # usable：现在能用；unusable：选项里有它但带错误码（见 unusable_reason）；
    # absent：本回合的选项里没有它（被动技能）；unknown：本回合的选项还没到
    option: Literal["usable", "unusable", "absent", "unknown"]
    unusable_reason: str | None = None  # 错误码，如 REQ_ENOUGH_MANA、REQ_NOT_EXHAUSTED_HERO_POWER
    targets: tuple[int, ...] = ()  # 现在可以选的目标实体
    # 使用时要不要选目标：这局最近一次能用时选项里有没有目标行；None 表示还没见过它能用
    needs_target: bool | None = None
    passive: bool = False  # 本回合第一组选项里就没有它：被动技能


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
    hero_power: HeroPower | None = None


class OpponentView(_Frozen):
    player_id: int
    hero_card_id: str | None
    health: int
    armor: int
    place: int | None
    tavern_tier: int | None
    last_board: tuple[Minion, ...] = ()
    last_seen_turn: int | None = None

    @property
    def alive(self) -> bool:
        """还在场上；淘汰后 health 为 0 或负数（护甲为 0）。"""
        return self.health > 0


class HeroOption(_Frozen):
    """选英雄阶段的一个备选英雄（我方手牌区里 CARDTYPE=HERO 的实体）。"""

    entity_id: int
    card_id: str
    position: int  # ZONE_POSITION：游戏里从左到右
    armor: int  # ARMOR 标签；没有就是 0（2022 年的日志大多没有）
    health: int  # HEALTH
    hero_power_dbf: int | None  # HERO_POWER 标签：英雄技能卡的 dbfId
    locked: bool = False  # BACON_LOCKED_MULLIGAN_HERO


class HeroSelect(_Frozen):
    offered: tuple[HeroOption, ...]  # 按位置排序，开局一般是 4 个
    reroll: bool = False  # 游戏实体的 BACON_MULLIGAN_HERO_REROLL_ACTIVE：本局可以重新随机英雄

    def signature(self) -> tuple[tuple[int, str], ...]:
        """(实体编号, 卡牌 ID)，按实体编号排序：重随后不管换的是实体还是卡牌 ID 都会变，
        只是位置变了（开局洗牌）则不变。"""
        return tuple(sorted((h.entity_id, h.card_id) for h in self.offered))


class ChoiceKind(StrEnum):
    HERO_POWER = "hero_power"  # 英雄技能的升级选项（凯瑞尔的姿态）
    TRINKET = "trinket"  # 饰品
    DARK_GIFT = "dark_gift"  # 黑暗之赐
    TRIPLE = "triple"  # 三连奖励的发现
    DISCOVER = "discover"  # 其他发现（随从战吼等）


class ChoiceOption(_Frozen):
    entity_id: int
    card_id: str
    card_type: str  # MINION / SPELL / BATTLEGROUND_TRINKET
    index: int  # 在选项里从左到右的序号，0 起（日志 Entities[] 的顺序）
    atk: int | None  # 只有随从有
    health: int | None
    tier: int | None
    races: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    golden: bool = False
    cost: int | None = None  # COST 标签（饰品的铸币价）；日志不写 0，所以没有就是 None
    gifts: tuple[str, ...] = ()  # 黑暗之赐：附着的赐福法术的卡牌 ID（查不到法术时是附魔的卡牌 ID）


class Choice(_Frozen):
    """游戏正弹着的"选一个"（2~4 个选项、选 1 个）。"""

    id: int  # 日志里的选择编号，本局内递增
    kind: ChoiceKind
    source_card_id: str  # 发起者的卡牌 ID（定罪、小型饰品、三连奖励……）
    options: tuple[ChoiceOption, ...]  # 按 index 排


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
    # 不查卡牌库的卡池粗估（衍生物也在里面、旧式金色卡没映射）；显示和 AI 用的是
    # advisor.pool.PoolEstimator 查过卡牌库的估计
    pool_estimate: dict[str, int] = {}
    uncertain: bool = False
    final_place: int | None = None
    global_dbfs: tuple[int, ...] = ()  # 本局全局机制（BACON_GLOBAL_*_DBID）的卡牌 dbfId
    # 只在选英雄阶段、我方手牌区里有备选英雄时不为 None；选完（英雄离开手牌区）就是 None
    hero_select: HeroSelect | None = None
    # 只在招募阶段、游戏正弹着一组还没选完的"选一个"时不为 None
    choice: Choice | None = None


class Event(_Frozen):
    type: EventType
    turn: int
    data: dict[str, int | str | None] = {}
