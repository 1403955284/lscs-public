"""按实体 ID 保存 tag，并维护按区域、按类型的索引，方便快速查询。
另外保存最近一组选项（现在能执行哪些操作），按操作的主实体查询。"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from hearthstone.enums import GameTag

from lscs.parsing.feed import (
    CardRevealed,
    GameMeta,
    GameStarted,
    OptionListed,
    OptionsStarted,
    PlayerDeclared,
    PowerChange,
    TagSet,
)


@dataclass
class ListedOption:
    """选项列表里的一个操作；读完这一组之前，目标还会陆续追加。"""

    error: str | None  # None：现在可以执行
    targets: list[int] = field(default_factory=list)  # 直接列在操作下面、error=NONE 的目标
    # 抉择：每一项（subOption）下面 error=NONE 的目标
    choice_targets: list[list[int]] = field(default_factory=list)

    def valid_targets(self) -> tuple[int, ...]:
        """可以选的目标，去重、保持顺序。抉择的目标只收对每一项都合法的
        （不知道玩家会选哪一项；有一项不需要目标时就没有这样的目标）。"""
        found = list(self.targets)
        if self.choice_targets:
            first, *rest = self.choice_targets
            found += [t for t in first if all(t in other for other in rest)]
        return tuple(dict.fromkeys(found))


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
        self.options: dict[int, ListedOption] = {}  # 最近一组选项：主实体 ID -> 选项
        self.options_turn: int | None = None  # 最近一组选项到达时游戏的 TURN
        self.first_listed: set[int] = set()  # options_turn 这个回合第一组选项里的主实体
        # 主实体最近一次以 error=NONE 列出时有没有目标行（整局保留）：
        # 带错误码的选项从不列目标，铸币不够时只能靠这里知道要不要目标
        self.needs_target: dict[int, bool] = {}
        self._option: ListedOption | None = None  # 正在读的选项，后面的目标行属于它
        self._option_entity: int | None = None
        self._first_group = False

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
            case OptionsStarted():
                turn = self.tag(self.game_entity_id, GameTag.TURN, 0)
                self._first_group = turn != self.options_turn
                if self._first_group:
                    self.first_listed = set()
                self.options = {}
                self.options_turn = turn
                self._option = None
            case OptionListed(kind="option", entity_id=entity_id, error=error):
                self._option = ListedOption(error)
                self._option_entity = entity_id
                if entity_id is not None:
                    self.options[entity_id] = self._option
                    if self._first_group:
                        self.first_listed.add(entity_id)
                    if error is None:
                        self.needs_target[entity_id] = False
            case OptionListed(kind="subOption"):
                if self._option is not None:
                    self._option.choice_targets.append([])
            case OptionListed(kind="target", entity_id=entity_id, error=error):
                option = self._option
                if option is None:
                    return
                if option.error is None and self._option_entity is not None:
                    self.needs_target[self._option_entity] = True
                if error is None and entity_id is not None:
                    # 跟在抉择的某一项后面的目标属于那一项（和 hslog 的嵌套方式一样）
                    bucket = option.choice_targets[-1] if option.choice_targets else option.targets
                    bucket.append(entity_id)

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
