from hearthstone.enums import GameTag, Zone

from lscs.parsing.feed import (
    CardRevealed,
    GameMeta,
    GameStarted,
    PlayerDeclared,
    PowerFeed,
    TagSet,
)

P = "D 10:00:00.0000000 GameState.DebugPrintPower() - "
HEADER = [
    P + "CREATE_GAME",
    P + "    GameEntity EntityID=1",
    P + "        tag=TURN value=0",
    P + "    Player EntityID=2 PlayerID=1 GameAccountId=[hi=1 lo=1]",
    P + "        tag=PLAYER_ID value=1",
    P + "    Player EntityID=3 PlayerID=2 GameAccountId=[hi=0 lo=0]",
    P + "        tag=PLAYER_ID value=2",
    P + "FULL_ENTITY - Creating ID=10 CardID=BG21_029",
    P + "    tag=ZONE value=PLAY",
]


def feed_all(feed, lines):
    out = []
    for line in lines:
        out.extend(feed.feed(line))
    return out


def test_header_produces_game_players_and_entity():
    changes = feed_all(PowerFeed(), HEADER)
    assert changes == [
        GameStarted(1),
        TagSet(1, GameTag.TURN, 0),
        PlayerDeclared(2, 1, is_ai=False),
        TagSet(2, GameTag.PLAYER_ID, 1),
        PlayerDeclared(3, 2, is_ai=True),
        TagSet(3, GameTag.PLAYER_ID, 2),
        CardRevealed(10, "BG21_029"),
        TagSet(10, GameTag.ZONE, Zone.PLAY),
    ]


def test_tags_arriving_later_for_open_entity_are_emitted():
    feed = PowerFeed()
    feed_all(feed, HEADER)
    # 最后一个 FULL_ENTITY 的 tag 行在下一次读取时才到
    assert feed.feed(P + "    tag=ATK value=3") == [TagSet(10, GameTag.ATK, 3)]
    assert feed.feed(P + "TAG_CHANGE Entity=10 tag=HEALTH value=2") == [
        TagSet(10, GameTag.HEALTH, 2)
    ]


def test_entity_references_by_bracket_and_game_entity():
    feed = PowerFeed()
    feed_all(feed, HEADER)
    line = (
        P
        + "TAG_CHANGE Entity=[entityName=Imp id=10 zone=PLAY zonePos=1 cardId=BG21_029 player=1] tag=ATK value=5"
    )
    assert feed.feed(line) == [TagSet(10, GameTag.ATK, 5)]
    assert feed.feed(P + "TAG_CHANGE Entity=GameEntity tag=TURN value=1") == [
        TagSet(1, GameTag.TURN, 1)
    ]


def test_hide_and_show_entity():
    feed = PowerFeed()
    feed_all(feed, HEADER)
    hide = (
        P
        + "HIDE_ENTITY - Entity=[entityName=Imp id=10 zone=PLAY zonePos=1 cardId=BG21_029 player=1] tag=ZONE value=DECK"
    )
    assert feed.feed(hide) == [TagSet(10, GameTag.ZONE, Zone.DECK)]
    show = P + "SHOW_ENTITY - Updating Entity=10 CardID=BGS_061"
    assert feed.feed(show) == [CardRevealed(10, "BGS_061")]


def test_game_meta_lines():
    feed = PowerFeed()
    assert feed.feed("D 10:00:00.0 GameState.DebugPrintGame() - GameType=GT_BATTLEGROUNDS") == [
        GameMeta("GameType", "GT_BATTLEGROUNDS")
    ]
    assert feed.feed("D 10:00:00.0 GameState.DebugPrintGame() - PlayerID=1, PlayerName=A#1") == []


def test_power_task_list_lines_are_ignored():
    feed = PowerFeed()
    feed_all(feed, HEADER)
    line = "D 10:00:00.0000000 PowerTaskList.DebugPrintPower() -     TAG_CHANGE Entity=10 tag=ATK value=9"
    assert feed.feed(line) == []


def test_bad_line_counts_error_and_keeps_going():
    feed = PowerFeed()
    feed_all(feed, HEADER)
    assert feed.feed("not a log line") == []
    assert feed.errors == 1
    assert feed.feed(P + "TAG_CHANGE Entity=10 tag=ATK value=1") == [TagSet(10, GameTag.ATK, 1)]


def test_hide_entity_with_unresolved_entity_is_dropped():
    feed = PowerFeed()
    feed_all(feed, HEADER)
    # HIDE_ENTITY with unresolvable entity should not crash
    hide = P + "HIDE_ENTITY - Entity=Bob tag=ZONE value=DECK"
    assert feed.feed(hide) == []
    assert feed.dropped == 1
    # Following normal TAG_CHANGE should still work
    assert feed.feed(P + "TAG_CHANGE Entity=10 tag=ATK value=1") == [TagSet(10, GameTag.ATK, 1)]


def test_packet_expansion_error_is_counted_and_skipped(monkeypatch):
    from hslog import packets

    feed = PowerFeed()
    feed_all(feed, HEADER)

    # Wrap the original _header method to raise on a specific packet
    original_header = feed._header

    def raising_header(packet):
        # Raise on TagChange with tag ATK and value 99
        if isinstance(packet, packets.TagChange):
            if hasattr(packet, "tag") and int(packet.tag) == int(GameTag.ATK):
                if hasattr(packet, "value") and int(packet.value) == 99:
                    raise RuntimeError("Simulated packet processing error")
        return original_header(packet)

    monkeypatch.setattr(feed, "_header", raising_header)

    # Feed a problematic TAG_CHANGE
    result = feed.feed(P + "TAG_CHANGE Entity=10 tag=ATK value=99")
    assert result == []
    assert feed.errors == 1

    # Following normal TAG_CHANGE should still work
    result = feed.feed(P + "TAG_CHANGE Entity=10 tag=ATK value=1")
    assert result == [TagSet(10, GameTag.ATK, 1)]
