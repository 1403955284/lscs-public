"""建议的数据模型（不可变）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from lscs.advisor.pool import PoolNote

ActionType = Literal["buy", "sell", "upgrade", "hero_power", "refresh", "freeze"]
# AI 建议的四路：快速建议、深度分析、选英雄、选项（游戏弹出的"选一个"）；
# review 是对局复盘（每局一次，只出现在调用记录里，不走调度器）
Lane = Literal["fast", "deep", "hero", "choice", "review"]
# 深度分析的操作为什么不能用了：refreshed 商店刷新了；invalid 操作不合法；
# changed 步骤已经做完、局面变化太大或已经进入战斗
Expired = Literal["refreshed", "invalid", "changed"]
ACTION_TYPES: tuple[str, ...] = ("buy", "sell", "upgrade", "hero_power", "refresh", "freeze")
# 站位建议里一个随从为什么要挪（站位建议设计文档第 3 节）：战斗型亡语、进击放最左，复仇放最右，
# 对面有劈砍时唯一的嘲讽靠边
PositionRole = Literal["deathrattle", "rally", "avenge", "taunt_edge"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Action(_Frozen):
    type: ActionType
    entity_id: int | None = None  # buy / sell 指向的实体；hero_power 需要目标时是目标
    reason: str = ""


class Plan(_Frozen):
    source: Literal["rules", "llm"]
    actions: tuple[Action, ...] = ()
    summary: str = ""
    direction: str | None = None
    confidence: float | None = None
    analysis: str = ""  # AI 的局势判断、本局方向、本回合目标
    look_for: tuple[str, ...] = ()  # AI 说接下来要找的牌

    def signature(self) -> tuple[tuple[str, int | None], ...]:
        """只看动作和对象、不看理由，用于判断两个方案是否相同。"""
        return tuple((a.type, a.entity_id) for a in self.actions)


class Advice(_Frozen):
    state_seq: int
    turn: int
    plan: Plan
    alternatives: tuple[Plan, ...] = ()
    stale_note: str | None = None
    elapsed: float | None = None  # AI 调用耗时（秒）
    lane: Lane = "fast"
    expired: Expired | None = None  # 深度分析的操作已经不能用，只看思路
    # 对子和要找的牌在池里还剩约几张（advisor.pool.pool_notes）；没有卡牌数据时为空
    pool_notes: tuple[PoolNote, ...] = ()


class HeroComment(_Frozen):
    entity_id: int
    text: str  # 一句点评，最多 20 字


class HeroAdvice(_Frozen):
    """选英雄建议（规则或 AI）。"""

    source: Literal["rules", "llm"]
    signature: tuple[tuple[int, str], ...]  # 基于哪一组备选（HeroSelect.signature()）
    pick: int | None  # 推荐的实体编号；规则拿不准时为 None
    reason: str
    comments: tuple[HeroComment, ...] = ()
    elapsed: float | None = None  # AI 调用耗时（秒）
    confidence: float | None = None  # AI 的把握，只记录不显示


class ChoiceAdvice(_Frozen):
    """AI 在游戏弹出的"选一个"里推荐的选项（没有规则版本）。"""

    choice_id: int  # 基于哪一组（Choice.id）
    pick: int  # 推荐的实体编号
    reason: str
    elapsed: float | None = None  # AI 调用耗时（秒）
    confidence: float | None = None  # AI 的把握，只记录不显示


class Move(_Frozen):
    """站位建议的一步：把一个随从拖到某个槽位。"""

    entity_id: int
    index: int  # 目标槽位，0 起（按做完前面几步之后的顺序）
    before_id: int | None  # 拖到谁的左边（拖完后紧挨在它右边的随从）；最左边 / 最右边时为 None
    role: PositionRole
    reason: str  # 终端的理由
    short: str  # 面板的短语


class PositionAdvice(_Frozen):
    """站位建议（规则，没有 AI 版本）。"""

    state_seq: int
    turn: int
    board: tuple[int, ...]  # 计算时我方场上的实体顺序（从左到右）
    target: tuple[int, ...]  # 目标顺序：board 的一个排列
    moves: tuple[Move, ...]  # 最多 MAX_MOVES 步；为空表示现在的顺序没问题
    more: int = 0  # 没显示的剩余步数
    cleave: str | None = None  # 下一个对手上次阵容里的劈砍随从（卡牌 ID）
    intro: bool = False  # 本局第一次有站位建议：终端多打一行机制说明
