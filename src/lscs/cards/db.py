"""卡牌数据库：读取 HearthstoneJSON 的 zhCN/cards.json。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from lscs.state.pool import GOLDEN_SUFFIX, base_card_id

_TAGS = re.compile(r"<[^>]+>")
_SEPARATOR = re.compile(r"[0-9@]{0,2}")
GOLDEN_MARKS = ("(金)", "（金）")  # 卡名后面的金色标记（AI 写的"要找"可能带着）
MIN_REPEAT = 6  # 重复的那一段至少这么长，避免把"+1+1"之类的短文本当成重复


def _collapse_repeat(text: str) -> str:
    """A + 分隔（最多两个数字或 @）+ A 收成 A。HearthstoneJSON 有的卡把说明写了两遍，
    例如凯瑞尔的"定罪"：A + "1" + A。"""
    for sep in range(3):
        if (len(text) - sep) % 2:
            continue
        half = (len(text) - sep) // 2
        if half < MIN_REPEAT:
            continue
        first, middle, second = text[:half], text[half : half + sep], text[half + sep :]
        if first == second and _SEPARATOR.fullmatch(middle):
            return first
    return text


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    text = _TAGS.sub("", text).replace("[x]", "").replace("\n", " ")
    return _collapse_repeat(re.sub(r"\s+", " ", text).strip())


@dataclass(frozen=True)
class CardInfo:
    card_id: str
    name: str
    text: str
    card_type: str
    tier: int | None
    races: tuple[str, ...]
    atk: int | None
    health: int | None
    is_pool_minion: bool
    is_pool_spell: bool
    duos_only: bool
    dbf_id: int | None = None
    cost: int | None = None  # 铸币费用（酒馆法术）
    normal_dbf_id: int | None = None  # battlegroundsNormalDbfId：金色卡对应的普通卡
    # 原始的 mechanics（DEATHRATTLE、BACON_RALLY、AVENGE……），排好序；没有就是空。
    # 不读 referencedTags：那是说明里提到的关键词（孢林卵怪的 TAUNT 是召唤出的触手的）
    mechanics: tuple[str, ...] = ()


class CardDB:
    def __init__(self, raw_cards: list[dict]) -> None:
        self._cards: dict[str, CardInfo] = {}
        self._by_dbf: dict[int, CardInfo] = {}
        for raw in raw_cards:
            races = raw.get("races") or ([raw["race"]] if raw.get("race") else [])
            info = CardInfo(
                card_id=raw["id"],
                name=raw.get("name", raw["id"]),
                text=clean_text(raw.get("text")),
                card_type=raw.get("type", ""),
                tier=raw.get("techLevel"),
                races=tuple(races),
                atk=raw.get("attack"),
                health=raw.get("health"),
                is_pool_minion=bool(raw.get("isBattlegroundsPoolMinion")),
                is_pool_spell=bool(raw.get("isBattlegroundsPoolSpell")),
                duos_only=bool(raw.get("isBattlegroundsDuosExclusive")),
                dbf_id=raw.get("dbfId"),
                cost=raw.get("cost"),
                normal_dbf_id=raw.get("battlegroundsNormalDbfId"),
                mechanics=tuple(sorted(raw.get("mechanics") or ())),
            )
            self._cards[info.card_id] = info
            if info.dbf_id is not None:
                self._by_dbf[info.dbf_id] = info
        self._pool_by_name: dict[str, CardInfo] | None = None  # 第一次按卡名查时才建

    @classmethod
    def from_json_file(cls, path: Path) -> CardDB:
        return cls(json.loads(path.read_text(encoding="utf-8")))

    def get(self, card_id: str) -> CardInfo | None:
        return self._cards.get(card_id) or self._cards.get(base_card_id(card_id))

    def get_by_dbf(self, dbf_id: int) -> CardInfo | None:
        return self._by_dbf.get(dbf_id)

    def name(self, card_id: str) -> str:
        info = self.get(card_id)
        if info is None:
            return card_id
        golden = card_id.endswith(GOLDEN_SUFFIX) and info.card_id != card_id
        return f"{info.name}(金)" if golden else info.name

    def normal_id(self, card_id: str) -> str:
        """金色卡对应的普通卡 ID：以 _G 结尾的去掉后缀；旧式金色卡（TB_BaconUps_*）按
        battlegroundsNormalDbfId 查；都不是（或查不到）时原样返回。"""
        if card_id.endswith(GOLDEN_SUFFIX):
            return base_card_id(card_id)
        info = self._cards.get(card_id)
        if info is not None and info.normal_dbf_id is not None:
            normal = self._by_dbf.get(info.normal_dbf_id)
            if normal is not None:
                return normal.card_id
        return card_id

    def find_pool_minion(self, name: str) -> CardInfo | None:
        """按卡名精确查卡池随从（去掉首尾空白和末尾的"(金)"）；同名多张取酒馆等级最低的。"""
        name = name.strip()
        for golden in GOLDEN_MARKS:
            name = name.removesuffix(golden).strip()
        if self._pool_by_name is None:
            index: dict[str, CardInfo] = {}
            for card in self.pool_minions():  # 按酒馆等级排好序：同名的先放进去的等级最低
                index.setdefault(card.name, card)
            self._pool_by_name = index
        return self._pool_by_name.get(name) if name else None

    def pool_minions(self, include_duos: bool = False) -> list[CardInfo]:
        return sorted(
            (
                c
                for c in self._cards.values()
                if c.is_pool_minion and (include_duos or not c.duos_only)
            ),
            key=lambda c: (c.tier or 0, c.card_id),
        )

    def pool_spells(self, include_duos: bool = False) -> list[CardInfo]:
        return sorted(
            (
                c
                for c in self._cards.values()
                if c.is_pool_spell and (include_duos or not c.duos_only)
            ),
            key=lambda c: (c.tier or 0, c.card_id),
        )
