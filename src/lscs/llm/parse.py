"""把大模型的回答解析成 Plan（M2 设计文档第 7.3 节）。"""

from __future__ import annotations

import json
import math

from lscs.advisor.models import ACTION_TYPES, Action, Plan

MAX_ACTIONS = 5  # 例如 买、买、英雄技能、刷新、冻结
MAX_LOOK_FOR = 6  # "要找的牌"最多几个
LOOK_FOR_CHARS = 20  # 每个最长几个字（超出截断，最后一个字换成"…"）


class ParseError(Exception):
    pass


class EmptyResponse(ParseError):
    pass


def _entity_id(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.lstrip("#").isdigit():
        return int(raw.lstrip("#"))
    return None


def _confidence(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return None
    try:
        value = float(raw)
    except OverflowError:
        return None
    return min(max(value, 0.0), 1.0) if math.isfinite(value) else None


def _text(raw: object) -> str:
    return raw.strip() if isinstance(raw, str) else ""


def _look_for(raw: object) -> tuple[str, ...]:
    """不是列表时忽略；跳过空的和不是文字的项。"""
    if not isinstance(raw, list):
        return ()
    names = []
    for item in raw:
        name = _text(item)
        if not name:
            continue
        if len(name) > LOOK_FOR_CHARS:
            name = name[: LOOK_FOR_CHARS - 1] + "…"
        names.append(name)
        if len(names) == MAX_LOOK_FOR:
            break
    return tuple(names)


def parse_plan(text: str) -> Plan:
    if not text.strip():
        raise EmptyResponse("空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ParseError("不是合法的 JSON") from None
    if not isinstance(data, dict):
        raise ParseError("不是 JSON 对象")
    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list):
        raise ParseError("缺少 actions 列表")
    if len(raw_actions) > MAX_ACTIONS:
        raise ParseError(f"动作超过 {MAX_ACTIONS} 个")
    actions = []
    for i, raw in enumerate(raw_actions, start=1):
        if not isinstance(raw, dict):
            raise ParseError(f"第 {i} 个动作不是对象")
        kind = raw.get("type")
        if kind not in ACTION_TYPES:
            raise ParseError(f"第 {i} 个动作类型未知：{kind}")
        entity = None
        if kind in ("buy", "sell"):
            entity = _entity_id(raw.get("entity_id"))
            if entity is None:
                raise ParseError(f"第 {i} 个动作缺少 entity_id")
        elif kind == "hero_power":
            entity = _entity_id(raw.get("entity_id"))  # 目标；不需要目标时没有，校验时也忽略
        actions.append(Action(type=kind, entity_id=entity, reason=_text(raw.get("reason"))))
    return Plan(
        source="llm",
        actions=tuple(actions),
        summary=_text(data.get("summary")),
        direction=_text(data.get("direction")) or None,
        confidence=_confidence(data.get("confidence")),
        analysis=_text(data.get("analysis")),
        look_for=_look_for(data.get("look_for")),
    )
