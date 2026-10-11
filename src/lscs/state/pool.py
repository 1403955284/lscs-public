"""卡池配置与剩余副本估算。"""

from __future__ import annotations

import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

GOLDEN_SUFFIX = "_G"


@dataclass(frozen=True)
class PoolConfig:
    tribe_count: int
    copies: dict[int, int]  # 酒馆等级 -> 每张随从的副本数


def load_pool_config(path: Path) -> PoolConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return PoolConfig(
        tribe_count=int(data["tribe_count"]),
        copies={int(tier): int(n) for tier, n in data["copies"].items()},
    )


class _Card(Protocol):
    card_id: str
    tier: int | None
    golden: bool


def base_card_id(card_id: str) -> str:
    return card_id.removesuffix(GOLDEN_SUFFIX)


def estimate_pool(
    copies: Mapping[int, int], held: Iterable[_Card], seen: Iterable[_Card] = ()
) -> dict[str, int]:
    """held 中的副本从池子里扣除（金色算 3 张）；seen 只登记卡牌、不扣除。"""
    tiers: dict[str, int] = {}
    taken: Counter[str] = Counter()
    for card in held:
        if card.tier is None:
            continue
        card_id = base_card_id(card.card_id)
        tiers[card_id] = card.tier
        golden = card.golden or card.card_id.endswith(GOLDEN_SUFFIX)
        taken[card_id] += 3 if golden else 1
    for card in seen:
        if card.tier is not None:
            tiers.setdefault(base_card_id(card.card_id), card.tier)
    return {
        card_id: max(copies[tier] - taken[card_id], 0)
        for card_id, tier in sorted(tiers.items())
        if tier in copies
    }
