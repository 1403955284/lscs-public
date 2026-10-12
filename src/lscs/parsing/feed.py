"""把 Power.log 行增量转换成扁平的实体变化。

hslog 负责解析每一行；它把结果组织成数据包树，适合事后整局导出。
实时场景需要"来一行、出一批变化"，所以这里包装 register_packet，
按注册顺序收集数据包，再展开成 TagSet 等扁平变化。
FULL_ENTITY 之类的包在注册之后还会陆续追加 tag= 行，
因此最后一个包保持"打开"，只输出新增的 tag。
选项列表（GameState.DebugPrintOptions，"现在能执行哪些操作"）也展开成扁平变化：
每组开头一个 OptionsStarted，之后每个选项、目标各一个 OptionListed。
"选一个"（GameState.DebugPrintEntityChoices：发现、饰品、选英雄……）和玩家的选择
（GameState.SendChoices）也是这样：包注册时只有头，发起者和选项随后面的行逐个追加，
所以最后一个包也保持"打开"，只输出新增的那几行。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from hearthstone.enums import GameTag
from hslog import LogParser, packets
from hslog.player import PlayerReference

log = logging.getLogger(__name__)

_GAME_META = re.compile(r"GameState\.DebugPrintGame\(\) - (\w+)=([^,\s]+)\s*$")
_CREATE_GAME = "GameState.DebugPrintPower() - CREATE_GAME"
# hslog 不注册选项组本身（只注册组里的每个选项），所以组的开头从原始行认出来
_OPTIONS_START = re.compile(r"GameState\.DebugPrintOptions\(\) - id=\d+\s*$")
_ENTITY_PACKETS = (packets.FullEntity, packets.ShowEntity, packets.ChangeEntity)
_CHOICE_PACKETS = (packets.Choices, packets.SendChoices)


@dataclass(frozen=True)
class GameStarted:
    game_entity_id: int


@dataclass(frozen=True)
class GameMeta:
    key: str
    value: str


@dataclass(frozen=True)
class PlayerDeclared:
    entity_id: int
    player_id: int
    is_ai: bool


@dataclass(frozen=True)
class CardRevealed:
    entity_id: int
    card_id: str


@dataclass(frozen=True)
class TagSet:
    entity_id: int
    tag: int
    value: int


@dataclass(frozen=True)
class OptionsStarted:
    """新的一组选项开始：之前的选项全部作废。"""


@dataclass(frozen=True)
class OptionListed:
    kind: str  # "option"（一个操作）、"subOption"（抉择的一项）或 "target"（可选的目标）
    entity_id: int | None  # 操作的主实体或目标实体；结束回合之类没有实体
    error: str | None  # None 表示 error=NONE（现在可以执行）；否则是错误码，如 REQ_ENOUGH_MANA


@dataclass(frozen=True)
class ChoiceOffered:
    """一组"选一个"的头（DebugPrintEntityChoices）：之前没选完的那组作废。"""

    id: int
    choice_type: str  # "GENERAL" / "MULLIGAN"
    max: int  # CountMax


@dataclass(frozen=True)
class ChoiceSource:
    """Source= 行：谁发起的。GameEntity（选英雄）时为 None。"""

    entity_id: int | None


@dataclass(frozen=True)
class ChoiceEntity:
    """一行 Entities[i]：一个选项，按日志里的顺序。"""

    entity_id: int


@dataclass(frozen=True)
class ChoiceSent:
    """玩家做出选择（SendChoices 的头）。"""

    id: int
    choice_type: str


@dataclass(frozen=True)
class ChoiceChosen:
    """一行 m_chosenEntities[i]：选中的实体。"""

    entity_id: int


PowerChange = (
    GameStarted
    | GameMeta
    | PlayerDeclared
    | CardRevealed
    | TagSet
    | OptionsStarted
    | OptionListed
    | ChoiceOffered
    | ChoiceSource
    | ChoiceEntity
    | ChoiceSent
    | ChoiceChosen
)


def _entity_id(entity: int | PlayerReference | None) -> int | None:
    if isinstance(entity, PlayerReference):
        return entity.entity_id
    return entity


def _choice_type(value: object) -> str:
    return getattr(value, "name", None) or str(value)


class PowerFeed:
    def __init__(self) -> None:
        self.errors = 0  # 解析失败的行数（跨对局累计）
        self.dropped = 0  # 无法确定实体 ID 而丢弃的变化数（跨对局累计）
        self._reset()

    def _reset(self) -> None:
        """换一个全新的解析器并清空数据包缓冲。"""
        self._parser = LogParser()
        self._packets: list[packets.Packet] = []
        self._next = 0
        self._tag_cursor = 0
        self._header_done = False
        self._choice_cursor = 0  # 打开的选择包已经输出了几个选项（或选中的实体）
        self._source_done = False  # 打开的选择包的发起者输出过没有
        self._game_id: int | None = None  # 游戏实体：Source=GameEntity 时发起者记为 None

        state = self._parser._parsing_state  # hslog 没有公开的数据包回调
        original = state.register_packet

        def register(packet, node=None):
            original(packet, node)
            self._packets.append(packet)

        state.register_packet = register

    def feed(self, line: str) -> list[PowerChange]:
        if _CREATE_GAME in line:
            # hslog 的玩家管理器不会随新对局重置：酒馆战棋的 PlayerID 是大厅座位号，
            # 每局可能不同，沿用旧解析器会抛 InconsistentPlayerIdError 导致整局没有玩家。
            # 同时丢弃上一局的数据包，避免内存随对局数无限增长。
            self._reset()
        try:
            self._parser.read_line(line)
        except Exception:
            self.errors += 1
            log.warning("无法解析日志行: %r", line, exc_info=True)
        changes = self._drain()
        m = _GAME_META.search(line)
        if m:
            changes.append(GameMeta(m.group(1), m.group(2)))
        elif _OPTIONS_START.search(line):
            changes.append(OptionsStarted())
        return changes

    def _drain(self) -> list[PowerChange]:
        out: list[PowerChange] = []
        while self._next < len(self._packets):
            packet = self._packets[self._next]
            try:
                if not self._header_done:
                    out.extend(self._header(packet))
                    self._header_done = True
                last = self._next == len(self._packets) - 1
                tags = getattr(packet, "tags", None)
                if tags is not None:
                    entity_id = _entity_id(packet.entity)
                    new_tags = tags[self._tag_cursor :]
                    if entity_id is None:
                        self.dropped += len(new_tags)
                    else:
                        out.extend(TagSet(entity_id, int(t), int(v)) for t, v in new_tags)
                    self._tag_cursor = len(tags)
                    if last:
                        break  # 后面的 tag= 行可能还会追加到这个包上
                elif isinstance(packet, _CHOICE_PACKETS):
                    out.extend(self._choice_rows(packet))
                    if last:
                        break  # 后面的 Source= / Entities[i] 行可能还会追加到这个包上
            except Exception:
                self.errors += 1
                log.warning("无法处理数据包: %r", packet, exc_info=True)
            self._next += 1
            self._tag_cursor = 0
            self._header_done = False
            self._choice_cursor = 0
            self._source_done = False
        return out

    def _choice_rows(self, packet: packets.Choices | packets.SendChoices) -> list[PowerChange]:
        """选择包里还没输出的行：发起者（只有 Choices 有）、选项或选中的实体。"""
        out: list[PowerChange] = []
        source = getattr(packet, "source", None)
        if source is not None and not self._source_done:
            self._source_done = True
            source_id = _entity_id(source)
            out.append(ChoiceSource(None if source_id == self._game_id else source_id))
        row = ChoiceEntity if isinstance(packet, packets.Choices) else ChoiceChosen
        for entity in packet.choices[self._choice_cursor :]:
            entity_id = _entity_id(entity)
            if entity_id is None:
                self.dropped += 1
            else:
                out.append(row(entity_id))
        self._choice_cursor = len(packet.choices)
        return out

    def _header(self, packet: packets.Packet) -> list[PowerChange]:
        if isinstance(packet, packets.CreateGame):
            self._game_id = int(packet.entity)
            return [GameStarted(self._game_id)]
        if isinstance(packet, packets.CreateGame.Player):
            entity_id = _entity_id(packet.entity)
            if entity_id is None:
                self.dropped += 1
                return []
            return [PlayerDeclared(entity_id, int(packet.player_id), packet.lo == 0)]
        if isinstance(packet, _ENTITY_PACKETS):
            entity_id = _entity_id(packet.entity)
            if entity_id is None or not packet.card_id:
                return []
            return [CardRevealed(entity_id, packet.card_id)]
        if isinstance(packet, packets.HideEntity):
            entity_id = _entity_id(packet.entity)
            if entity_id is None:
                self.dropped += 1
                return []
            return [TagSet(entity_id, int(GameTag.ZONE), int(packet.zone))]
        if isinstance(packet, packets.TagChange):
            entity_id = _entity_id(packet.entity)
            if entity_id is None:
                self.dropped += 1
                return []
            return [TagSet(entity_id, int(packet.tag), int(packet.value))]
        if isinstance(packet, packets.Choices):
            return [ChoiceOffered(int(packet.id), _choice_type(packet.type), int(packet.max))]
        if isinstance(packet, packets.SendChoices):
            return [ChoiceSent(int(packet.id), _choice_type(packet.type))]
        if isinstance(packet, packets.Option):
            entity_id = _entity_id(packet.entity)
            return [
                OptionListed(
                    str(packet.optype),
                    entity_id if isinstance(entity_id, int) else None,
                    None if packet.error is None else str(packet.error),
                )
            ]
        return []
