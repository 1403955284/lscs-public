"""商店里的核心卡（流派指南自动更新设计文档第 5.1 节）。

有整局计划且有核心卡（"未定"时写的"想找"的卡也算）时，核心卡就是计划的卡；没有时是本局种族能走
的流派（流派指南里有种族标签的段，通用运营不算）的核心卡，按"在几段里"从多到少、"第一次出现的段"
从前到后排，商店里最多标 MAX_CORE_NAMES 个名字（同名多张都标）。流派只算流派指南块里放得下的那些
（llm.comps.kept_comps）：AI 没收到的流派，它的核心卡也不标。只标商店里的随从（金色也标），
不标法术、手牌、场面。它是"留意"，不是"必买"：不改规则建议、AI 的方案和"下一步"。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lscs.advisor.context import MINION
from lscs.advisor.models import GamePlan
from lscs.advisor.plan_check import plain_name
from lscs.llm.comps import CompsFile, kept_comps
from lscs.state.models import GameState

Namer = Callable[[str], str]
CORE = "◆"
MAX_CORE_NAMES = 3  # 没有计划时最多标几个名字（同名多张都标）
MAX_WHY = 2  # why 里最多写几个流派名
PLAN_WHY = "计划"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class CoreMark(_Frozen):
    entity_id: int
    index: int  # 商店里从左数第几个，0 起
    name: str  # 卡名（金色也写普通卡名）
    why: str  # "计划" 或 "鱼人·下水道逃生鱼人、鱼人·怒胆喷毒鱼人"（最多两个流派名）


class CoreMarks(_Frozen):
    turn: int
    shop: tuple[int, ...]  # 算的时候商店里的实体编号（面板按它判断还是不是这批商店）
    marks: tuple[CoreMark, ...]  # 按商店位置
    names: tuple[str, ...]  # 这局现在算"核心"的卡名（选项标记用）
    source: Literal["plan", "guides"]
    whys: tuple[str, ...] = ()  # names 每个的 why（终端写选项里的核心卡用）

    def why(self, card: str) -> str:
        """names 里一张卡的 why；不在里面时为空。"""
        return dict(zip(self.names, self.whys, strict=False)).get(card, "")


def _from_plan(game_plan: GamePlan | None) -> bool:
    return game_plan is not None and bool(game_plan.key_cards)


def core_names(
    game_plan: GamePlan | None, comps: CompsFile | None, tribes: frozenset[str]
) -> tuple[tuple[str, str], ...]:
    """(卡名, why)，按优先顺序：计划的核心卡按计划里的顺序；指南的按（在几段里 多→少，第一次
    出现的段 前→后），只看流派指南块里放得下的段。本局种族还不知道、没有计划也没有指南时为空。"""
    if game_plan is not None and _from_plan(game_plan):
        return tuple((card, PLAN_WHY) for card in game_plan.key_cards)
    if comps is None or not tribes:
        return ()
    sections: dict[str, list[str]] = {}  # 卡名 → 在哪几段（按段的顺序）
    first: dict[str, int] = {}
    order = 0
    for guide in kept_comps(comps, tribes)[0]:
        if guide.general or not guide.tribes:
            continue
        for card in guide.core_cards:
            if card not in sections:
                sections[card] = []
                first[card] = order
                order += 1
            if guide.name not in sections[card]:
                sections[card].append(guide.name)
    ranked = sorted(sections, key=lambda card: (-len(sections[card]), first[card]))
    return tuple((card, "、".join(sections[card][:MAX_WHY])) for card in ranked)


def core_marks(
    state: GameState, game_plan: GamePlan | None, comps: CompsFile | None, name: Namer
) -> CoreMarks | None:
    """商店里的核心卡；这局现在没有核心卡（没有计划也没有指南、种族还不知道）时 None。商店里一张
    都没有时照样返回（marks 为空，选项标记要用 names）。"""
    names = core_names(game_plan, comps, state.tribes)
    if not names:
        return None
    why = dict(names)
    rank = {card: i for i, (card, _) in enumerate(names)}
    found = [
        (index, item.entity_id, card)
        for index, item in enumerate(state.shop)
        if item.card_type == MINION and (card := plain_name(name(item.card_id))) in why
    ]
    source: Literal["plan", "guides"] = "plan" if _from_plan(game_plan) else "guides"
    if source == "guides":
        kept = sorted({card for _, _, card in found}, key=rank.__getitem__)[:MAX_CORE_NAMES]
        found = [entry for entry in found if entry[2] in kept]
    return CoreMarks(
        turn=state.turn,
        shop=tuple(item.entity_id for item in state.shop),
        marks=tuple(
            CoreMark(entity_id=entity, index=index, name=card, why=why[card])
            for index, entity, card in found
        ),
        names=tuple(card for card, _ in names),
        source=source,
        whys=tuple(reason for _, reason in names),
    )
