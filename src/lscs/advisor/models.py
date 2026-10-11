"""建议的数据模型（不可变）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ActionType = Literal["buy", "sell", "upgrade", "refresh", "freeze"]
ACTION_TYPES: tuple[str, ...] = ("buy", "sell", "upgrade", "refresh", "freeze")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Action(_Frozen):
    type: ActionType
    entity_id: int | None = None  # buy / sell 指向的实体
    reason: str = ""


class Plan(_Frozen):
    source: Literal["rules", "llm"]
    actions: tuple[Action, ...] = ()
    summary: str = ""
    direction: str | None = None
    confidence: float | None = None

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
