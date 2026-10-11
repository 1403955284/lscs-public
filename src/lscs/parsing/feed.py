"""把 Power.log 行增量转换成扁平的实体变化。

hslog 负责解析每一行；它把结果组织成数据包树，适合事后整局导出。
实时场景需要"来一行、出一批变化"，所以这里包装 register_packet，
按注册顺序收集数据包，再展开成 TagSet 等扁平变化。
FULL_ENTITY 之类的包在注册之后还会陆续追加 tag= 行，
因此最后一个包保持"打开"，只输出新增的 tag。
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
_ENTITY_PACKETS = (packets.FullEntity, packets.ShowEntity, packets.ChangeEntity)


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


PowerChange = GameStarted | GameMeta | PlayerDeclared | CardRevealed | TagSet


def _entity_id(entity: int | PlayerReference | None) -> int | None:
    if isinstance(entity, PlayerReference):
        return entity.entity_id
    return entity


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
        return changes

    def _drain(self) -> list[PowerChange]:
        out: list[PowerChange] = []
        while self._next < len(self._packets):
            packet = self._packets[self._next]
            try:
                if not self._header_done:
                    out.extend(self._header(packet))
                    self._header_done = True
                tags = getattr(packet, "tags", None)
                if tags is not None:
                    entity_id = _entity_id(packet.entity)
                    new_tags = tags[self._tag_cursor :]
                    if entity_id is None:
                        self.dropped += len(new_tags)
                    else:
                        out.extend(TagSet(entity_id, int(t), int(v)) for t, v in new_tags)
                    self._tag_cursor = len(tags)
                    if self._next == len(self._packets) - 1:
                        break  # 后面的 tag= 行可能还会追加到这个包上
            except Exception:
                self.errors += 1
                log.warning("无法处理数据包: %r", packet, exc_info=True)
            self._next += 1
            self._tag_cursor = 0
            self._header_done = False
        return out

    def _header(self, packet: packets.Packet) -> list[PowerChange]:
        if isinstance(packet, packets.CreateGame):
            return [GameStarted(int(packet.entity))]
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
        return []
