"""按实体 ID 保存 tag，并维护按区域、按类型的索引，方便快速查询。"""

from __future__ import annotations

from collections import defaultdict

from hearthstone.enums import GameTag

from lscs.parsing.feed import (
    CardRevealed,
    GameMeta,
    GameStarted,
    PlayerDeclared,
    PowerChange,
    TagSet,
)


class EntityStore:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.game_entity_id: int | None = None
        self.meta: dict[str, str] = {}
        self.players: dict[int, int] = {}  # player_id -> 玩家实体 ID
        self.ai_players: set[int] = set()  # AI 玩家（Bob）的 player_id
        self._tags: dict[int, dict[int, int]] = {}
        self._card_ids: dict[int, str] = {}
        self._zone_index: dict[tuple[int, int], set[int]] = defaultdict(set)
        self._zone_key: dict[int, tuple[int, int]] = {}
        self._type_index: dict[int, set[int]] = defaultdict(set)
        self._type_key: dict[int, int] = {}

    def apply(self, change: PowerChange) -> None:
        match change:
            case GameStarted(game_entity_id=game_id):
                self.reset()
                self.game_entity_id = game_id
            case GameMeta(key=key, value=value):
                self.meta[key] = value
            case PlayerDeclared(entity_id=entity_id, player_id=player_id, is_ai=is_ai):
                self.players[player_id] = entity_id
                if is_ai:
                    self.ai_players.add(player_id)
            case CardRevealed(entity_id=entity_id, card_id=card_id):
                self._card_ids[entity_id] = card_id
            case TagSet(entity_id=entity_id, tag=tag, value=value):
                self._tags.setdefault(entity_id, {})[tag] = value
                if tag in (GameTag.ZONE, GameTag.CONTROLLER):
                    self._reindex_zone(entity_id)
                elif tag == GameTag.CARDTYPE:
                    self._reindex_type(entity_id)

    def tag(self, entity_id: int | None, tag: int, default: int | None = None) -> int | None:
        if entity_id is None:
            return default
        return self._tags.get(entity_id, {}).get(tag, default)

    def tags_of(self, entity_id: int) -> dict[int, int]:
        return self._tags.get(entity_id, {})

    def card_id(self, entity_id: int | None) -> str | None:
        if entity_id is None:
            return None
        return self._card_ids.get(entity_id)

    def player_entity(self, player_id: int | None) -> int | None:
        if player_id is None:
            return None
        return self.players.get(player_id)

    def in_zone(self, controller: int, zone: int) -> list[int]:
        return sorted(self._zone_index.get((controller, zone), ()))

    def of_type(self, card_type: int) -> list[int]:
        return sorted(self._type_index.get(card_type, ()))

    def _reindex_zone(self, entity_id: int) -> None:
        old = self._zone_key.pop(entity_id, None)
        if old is not None:
            self._zone_index[old].discard(entity_id)
        tags = self._tags[entity_id]
        if GameTag.ZONE in tags and GameTag.CONTROLLER in tags:
            key = (tags[GameTag.CONTROLLER], tags[GameTag.ZONE])
            self._zone_index[key].add(entity_id)
            self._zone_key[entity_id] = key

    def _reindex_type(self, entity_id: int) -> None:
        old = self._type_key.pop(entity_id, None)
        if old is not None:
            self._type_index[old].discard(entity_id)
        card_type = self._tags[entity_id][GameTag.CARDTYPE]
        self._type_index[card_type].add(entity_id)
        self._type_key[entity_id] = card_type
