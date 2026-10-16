"""把大模型的回答解析成 Plan（M2 设计文档第 7.3 节），选英雄的回答解析成 HeroAdvice
（选英雄建议设计文档第 5.3 节），选项的回答解析成 ChoiceAdvice（选项建议设计文档第 5.3 节）。"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Sequence

from lscs.advisor.models import (
    ACTION_TYPES,
    STAGES,
    UNDECIDED,
    Action,
    ChoiceAdvice,
    GamePlan,
    HeroAdvice,
    HeroComment,
    Plan,
)
from lscs.advisor.names import TRIBE_ZH
from lscs.advisor.plan_check import plain_name

MAX_ACTIONS = 5  # 例如 买、买、英雄技能、刷新、冻结
MAX_LOOK_FOR = 6  # "要找的牌"最多几个
LOOK_FOR_CHARS = 20  # 每个最长几个字（超出截断，最后一个字换成"…"）
HERO_REASON_CHARS = 60  # 选英雄的理由最长几个字（截断方式同上）
HERO_COMMENT_CHARS = 20  # 每个英雄的点评最长几个字
CHOICE_REASON_CHARS = 30  # 选项的理由最长几个字（提示词要求 25 字以内）
NO_REASON = "（AI 没有给理由）"
# 整局计划（整局计划与流派指南设计文档第 4.3 节）：每个字段都要短，面板放得下
PLAN_COMP_CHARS = 12  # 目标流派
PLAN_KEY_CARDS = 3  # 核心卡最多几张（每张最长 LOOK_FOR_CHARS 个字）
PLAN_TEXT_CHARS = 40  # 升本节奏、刷新规则、换方向条件、改计划的原因
_ZH_TRIBES = {zh: race for race, zh in TRIBE_ZH.items() if race != "ALL"}
# 系统提示示例里 plan 的占位说明（prompt.EXAMPLE 用的就是它）：AI 原样抄回来时当作没写，
# 流派名是占位说明时当作"未定"
PLAN_PLACEHOLDERS: dict[str, object] = {
    "comp": "目标流派（指南里的名字；还没定写未定）",
    "tribe": "主种族（中文，如 畸变）",
    "key_cards": ["核心卡名", "核心卡名"],
    "tiers": "升本节奏一句话，例如：第 6 回合 4 本，第 8 回合 5 本",
    "roll": "刷新和保血的规则一句话",
    "stage": "前期/过渡/成型/保血",
    "pivot": "换方向的条件一句话",
    "changed": "改了计划才写一句原因，没改留空",
}
PLACEHOLDER_COMP = "目标流派"  # 占位说明去掉括号后剩下的
Canonical = Callable[[str], str | None]  # 流派名 → 流派指南的标题；对不上时 None
PLACEHOLDER_CARD = "核心卡名"
# 流派名末尾的括号（"畸变怪养神流（亡语/弃牌）""畸变神明（畸变）"）：截断前去掉，免得只剩半个括号
_TRAILING_NOTE = re.compile(r"\s*[（(][^（）()]*[）)]\s*$")


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


def _cut(text: str, limit: int) -> str:
    """超出 limit 个字时截断，最后一个字换成"…"。"""
    return text[: limit - 1] + "…" if len(text) > limit else text


def _one_line(raw: object, limit: int) -> str:
    return _cut(" ".join(_text(raw).split()), limit)


def _json_object(text: str) -> dict:
    if not text.strip():
        raise EmptyResponse("空内容")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise ParseError("不是合法的 JSON") from None
    if not isinstance(data, dict):
        raise ParseError("不是 JSON 对象")
    return data


def _look_for(raw: object) -> tuple[str, ...]:
    """不是列表时忽略；跳过空的和不是文字的项。"""
    if not isinstance(raw, list):
        return ()
    names = []
    for item in raw:
        name = _text(item)
        if not name:
            continue
        names.append(_cut(name, LOOK_FOR_CHARS))
        if len(names) == MAX_LOOK_FOR:
            break
    return tuple(names)


def _card_name(raw: object) -> str:
    """去掉 AI 从局面里抄来的 ✓ 和末尾的"(金)"。"""
    return _cut(plain_name(_one_line(raw, 10_000)), LOOK_FOR_CHARS)


def _comp_name(raw: object, canonical: Canonical | None = None) -> str:
    """目标流派：去掉末尾的括号说明（一层层去，去光了就保留原样），再截到 PLAN_COMP_CHARS。
    canonical（流派名对到流派指南的标题）在截断之前用：HSReplay 的英文名比 PLAN_COMP_CHARS 长，
    截断后就对不上别名了（流派指南自动更新设计文档第 3.3 节）；占位说明和"未定"不对。"""
    name = _one_line(raw, 10_000)
    while True:
        shorter = _TRAILING_NOTE.sub("", name)
        if shorter == name or not shorter:
            break
        name = shorter
    cut = _cut(name, PLAN_COMP_CHARS)
    if canonical is not None and cut not in ("", PLACEHOLDER_COMP, UNDECIDED):
        found = canonical(name)
        if found:
            return _cut(found, PLAN_COMP_CHARS)
    return cut


def _key_cards(raw: object) -> tuple[str, ...]:
    """和 look_for 一样读：不是列表时没有，跳过空的和不是文字的；去掉"(金)"、重复的，最多 3 张。"""
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for item in raw:
        name = _card_name(item)
        if name and name != PLACEHOLDER_CARD and name not in names:
            names.append(name)
        if len(names) == PLAN_KEY_CARDS:
            break
    return tuple(names)


def _plan_tribe(raw: object) -> str | None:
    """中文种族名反查成枚举名（"畸变" → ABERRATION），也认枚举名；查不到（含"全种族"）时 None。"""
    text = _one_line(raw, 20)
    if text.upper() in TRIBE_ZH and text.upper() != "ALL":
        return text.upper()
    return _ZH_TRIBES.get(text)


def _game_plan(raw: object, canonical: Canonical | None = None) -> GamePlan | None:
    """回答里的 plan：不是对象、没有 comp 时为 None（不算错，不触发改正）。"""
    if not isinstance(raw, dict):
        return None
    comp = _comp_name(raw.get("comp"), canonical)
    if not comp:
        return None
    if comp == PLACEHOLDER_COMP:
        comp = UNDECIDED  # 原样抄了示例的占位说明：还没定

    def text(field: str) -> str:
        value = _one_line(raw.get(field), PLAN_TEXT_CHARS)
        return "" if value == _one_line(PLAN_PLACEHOLDERS[field], PLAN_TEXT_CHARS) else value

    stage = raw.get("stage")
    return GamePlan(
        comp=comp,
        tribe=_plan_tribe(raw.get("tribe")),
        key_cards=_key_cards(raw.get("key_cards")),
        tiers=text("tiers"),
        roll=text("roll"),
        stage=stage if stage in STAGES else None,
        pivot=text("pivot"),
        changed=text("changed"),
    )


def parse_plan(text: str, canonical: Canonical | None = None) -> Plan:
    """canonical：把 AI 写的流派名对到流派指南的标题（llm.comps.match_comp），对不上时返回 None；
    在截断到 PLAN_COMP_CHARS 之前用。"""
    data = _json_object(text)
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
        game_plan=_game_plan(data.get("plan"), canonical),
    )


def _hero_comments(raw: object, offered: Sequence[int]) -> tuple[HeroComment, ...]:
    """不是列表时当作没有；跳过不是对象、编号不在备选里、点评为空的项；同一个英雄只留第一条。"""
    if not isinstance(raw, list):
        return ()
    comments: dict[int, HeroComment] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        entity = _entity_id(item.get("entity_id"))
        text = _one_line(item.get("comment"), HERO_COMMENT_CHARS)
        if entity is None or entity not in offered or entity in comments or not text:
            continue
        comments[entity] = HeroComment(entity_id=entity, text=text)
    return tuple(comments.values())


def parse_hero_pick(
    text: str, offered: Sequence[int], signature: tuple[tuple[int, str], ...] = ()
) -> HeroAdvice:
    """offered：备选英雄的实体编号；signature：这组备选的签名，原样放进结果。"""
    data = _json_object(text)
    pick = _entity_id(data.get("pick"))
    if pick is None:
        raise ParseError("缺少 pick（推荐的英雄编号）")
    if pick not in offered:
        raise ParseError("推荐的英雄不在备选里")
    return HeroAdvice(
        source="llm",
        signature=signature,
        pick=pick,
        reason=_one_line(data.get("reason"), HERO_REASON_CHARS) or NO_REASON,
        comments=_hero_comments(data.get("heroes"), offered),
        confidence=_confidence(data.get("confidence")),
    )


def parse_choice_pick(text: str, offered: Sequence[int], choice_id: int = 0) -> ChoiceAdvice:
    """offered：这组选项的实体编号；choice_id：这组的编号，原样放进结果。"""
    data = _json_object(text)
    pick = _entity_id(data.get("pick"))
    if pick is None:
        raise ParseError("缺少 pick（推荐的选项编号）")
    if pick not in offered:
        raise ParseError("推荐的选项不在选项里")
    return ChoiceAdvice(
        choice_id=choice_id,
        pick=pick,
        reason=_one_line(data.get("reason"), CHOICE_REASON_CHARS) or NO_REASON,
        confidence=_confidence(data.get("confidence")),
    )
