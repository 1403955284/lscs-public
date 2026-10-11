"""读取 strategy/current/rules.toml。"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_SKIN = re.compile(r"_SKIN_\w+$")


def base_hero_id(hero_card_id: str) -> str:
    """去掉皮肤后缀：TB_BaconShop_HERO_49_SKIN_A -> TB_BaconShop_HERO_49。"""
    return _SKIN.sub("", hero_card_id)


@dataclass(frozen=True)
class RulesConfig:
    curve: dict[int, int]  # 回合 -> 目标酒馆等级
    greedy_at: int
    safe_below: int
    buy_cost_default: int
    keyword_score: int
    tribe_score: int
    spell_score: int
    buy_cost_heroes: dict[str, int] = field(default_factory=dict)

    def buy_cost_for(self, hero_card_id: str | None) -> int:
        if hero_card_id is None:
            return self.buy_cost_default
        return self.buy_cost_heroes.get(base_hero_id(hero_card_id), self.buy_cost_default)

    def target_tier(self, turn: int, effective_health: int) -> int:
        """节奏表里到这个回合应达到的等级；血量健康时提前一回合。"""
        if effective_health >= self.greedy_at:
            turn += 1
        reached = [tier for t, tier in self.curve.items() if t <= turn]
        return max(reached, default=1)


def load_rules_config(path: Path) -> RulesConfig:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    buy_cost = data["buy_cost"]
    return RulesConfig(
        curve={int(t): int(tier) for t, tier in data["curve"].items()},
        greedy_at=int(data["health"]["greedy_at"]),
        safe_below=int(data["health"]["safe_below"]),
        buy_cost_default=int(buy_cost["default"]),
        buy_cost_heroes={str(k): int(v) for k, v in buy_cost.get("heroes", {}).items()},
        keyword_score=int(data["score"]["keyword"]),
        tribe_score=int(data["score"]["tribe"]),
        spell_score=int(data["score"]["spell"]),
    )
