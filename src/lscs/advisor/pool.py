"""查过卡牌库的卡池估计（对手一览与卡池估计设计文档第 4 节）。

状态层的 GameState.pool_estimate 不查卡牌库，衍生物也在里面、旧式金色卡没映射；这里按卡牌库只算
卡池随从，金色卡（_G 后缀、只有 PREMIUM、旧式 TB_BaconUps_*）都映射到普通卡、算 3 张，本局种族
确认后只算本局的牌。张数 = 每级份数 − 我方场面和手牌 − 还没淘汰的对手上次的阵容；商店里的牌刷新后
回池，不扣。终端、面板和 AI 的"还剩约 N 张"都从这里来。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from lscs.cards.db import GOLDEN_MARKS, CardDB, CardInfo
from lscs.state.build import held_minions
from lscs.state.models import GameState, TribesConfidence
from lscs.state.pool import GOLDEN_SUFFIX

if TYPE_CHECKING:
    from lscs.advisor.context import TurnContext

Namer = Callable[[str], str]
MAX_NOTES = 8  # 最多写几张牌的张数


def in_lobby(card: CardInfo, tribes: frozenset[str]) -> bool:
    """本局种族的随从、中立随从（没有种族）和全种族随从。"""
    return not card.races or "ALL" in card.races or bool(tribes.intersection(card.races))


@dataclass(frozen=True)
class PoolView:
    """查过卡牌库的卡池估计。taken：普通卡 ID -> 已扣除张数（我方场面 + 手牌、未淘汰对手的
    上次阵容；金色算 3）；只含卡池随从。"""

    copies: Mapping[int, int]  # 酒馆等级 -> 每张的份数（pool.toml）
    taken: Mapping[str, int]
    tribes: frozenset[str]  # 本局种族；confirmed 为假时不按种族过滤
    confirmed: bool
    cards: CardDB

    def pool_card(self, card_id: str) -> CardInfo | None:
        """这张牌（金色映射到普通卡）在本局卡池里时返回普通卡的数据，否则 None。"""
        info = self.cards.get(self.cards.normal_id(card_id))
        if info is None or not info.is_pool_minion or info.tier not in self.copies:
            return None
        if self.confirmed and self.tribes and not in_lobby(info, self.tribes):
            return None
        return info

    def remaining(self, card_id: str) -> int | None:
        """还剩约几张；不是卡池随从、卡牌库不认识、等级不在 copies 里、或本局种族已确认而
        它不在本局时 None。"""
        info = self.pool_card(card_id)
        if info is None or info.tier is None:
            return None
        return max(self.copies[info.tier] - self.taken.get(info.card_id, 0), 0)


@dataclass(frozen=True)
class PoolNote:
    card_id: str  # 普通卡 ID
    name: str
    have: int  # 我方场面 + 手牌里的非金色张数（TurnContext.copies）
    left: int  # remaining()
    shop: int = 0  # 商店里现在有几张

    @property
    def pair(self) -> bool:
        """对子（有 2 张以上），或者差一张成对子（有 1 张、商店里也有）。"""
        return self.have >= 2 or (self.have == 1 and self.shop >= 1)


class PoolEstimator:
    def __init__(self, cards: CardDB, copies: Mapping[int, int]) -> None:
        self._cards = cards
        self._copies = dict(copies)

    def view(self, state: GameState) -> PoolView:
        """酒馆等级用卡牌库的 tier，不用实体标签。"""
        view = PoolView(
            copies=self._copies,
            taken={},
            tribes=state.tribes,
            confirmed=state.tribes_confidence == TribesConfidence.CONFIRMED,
            cards=self._cards,
        )
        taken: Counter[str] = Counter()
        for card in held_minions(state.me, state.opponents):
            info = view.pool_card(card.card_id)
            if info is None:
                continue
            golden = (
                card.golden
                or card.card_id.endswith(GOLDEN_SUFFIX)
                or self._cards.normal_id(card.card_id) != card.card_id
            )
            taken[info.card_id] += 3 if golden else 1
        return replace(view, taken=dict(taken))


def pool_notes(
    view: PoolView,
    ctx: TurnContext | None,
    shop_ids: Iterable[str],
    look_for: Sequence[str],
    name: Namer,
) -> tuple[PoolNote, ...]:
    """显示和提示词共用的"值得写张数的牌"，按下面的顺序、去重、最多 8 条，remaining() 为 None
    的不列：
    1. 对子：ctx.copies ≥ 2 的；
    2. 差一张成对子：ctx.copies == 1 且商店里也有一张的；
    3. look_for 里能按名字查到的卡池随从（AI 的要找 / 记忆里的要找）。
    没有 ctx（不在招募阶段）时为空。"""
    if ctx is None:
        return ()
    cards = view.cards
    have: Counter[str] = Counter()
    for card_id, count in ctx.copies.items():
        have[cards.normal_id(card_id)] += count
    in_shop = Counter(cards.normal_id(card_id) for card_id in shop_ids)
    pairs = sorted((c for c in have if have[c] >= 2), key=lambda c: -have[c])
    near = [c for c in have if have[c] == 1 and in_shop[c]]
    wanted = []
    for text in look_for:
        info = cards.find_pool_minion(text)
        if info is not None:
            wanted.append(info.card_id)
    notes: list[PoolNote] = []
    seen: set[str] = set()
    for card_id in (*pairs, *near, *wanted):
        if len(notes) >= MAX_NOTES:
            break
        if card_id in seen:
            continue
        seen.add(card_id)
        left = view.remaining(card_id)
        if left is None:
            continue
        notes.append(PoolNote(card_id, name(card_id), have[card_id], left, in_shop[card_id]))
    return tuple(notes)


def note_for(text: str, notes: Iterable[PoolNote]) -> PoolNote | None:
    """AI 写的"要找"（卡名，可能带"(金)"）对应的那一条。"""
    text = text.strip()
    for golden in GOLDEN_MARKS:
        text = text.removesuffix(golden).strip()
    return next((n for n in notes if n.name == text), None)
