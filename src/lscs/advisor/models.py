"""建议的数据模型（不可变）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ActionType = Literal["buy", "sell", "upgrade", "hero_power", "refresh", "freeze"]
Lane = Literal["fast", "deep", "hero"]  # AI 建议的三路：快速建议、深度分析、选英雄
# 深度分析的操作为什么不能用了：refreshed 商店刷新了；invalid 操作不合法；
# changed 步骤已经做完、局面变化太大或已经进入战斗
Expired = Literal["refreshed", "invalid", "changed"]
ACTION_TYPES: tuple[str, ...] = ("buy", "sell", "upgrade", "hero_power", "refresh", "freeze")


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
