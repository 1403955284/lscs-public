from hearthstone.enums import CardType, GameTag, Zone

from lscs.parsing.feed import CardRevealed, GameMeta, GameStarted, PlayerDeclared, TagSet
from lscs.state.entities import EntityStore


def make_store():
    store = EntityStore()
    for change in [
        GameStarted(1),
        GameMeta("GameType", "GT_BATTLEGROUNDS"),
        PlayerDeclared(2, 1, is_ai=False),
        PlayerDeclared(3, 2, is_ai=True),
        CardRevealed(10, "BG21_029"),
        TagSet(10, GameTag.CONTROLLER, 2),
        TagSet(10, GameTag.ZONE, Zone.PLAY),
        TagSet(10, GameTag.CARDTYPE, CardType.MINION),
    ]:
        store.apply(change)
    return store


def test_basic_lookups():
    store = make_store()
    assert store.game_entity_id == 1
    assert store.meta == {"GameType": "GT_BATTLEGROUNDS"}
    assert store.player_entity(1) == 2
    assert store.ai_players == {2}
    assert store.card_id(10) == "BG21_029"
    assert store.tag(10, GameTag.ZONE) == Zone.PLAY
    assert store.tag(10, GameTag.ATK, 0) == 0
    assert store.tag(None, GameTag.ATK, 7) == 7


def test_zone_index_follows_controller_and_zone_changes():
    store = make_store()
    assert store.in_zone(2, Zone.PLAY) == [10]
    store.apply(TagSet(10, GameTag.CONTROLLER, 1))
    store.apply(TagSet(10, GameTag.ZONE, Zone.HAND))
    assert store.in_zone(2, Zone.PLAY) == []
    assert store.in_zone(1, Zone.HAND) == [10]


def test_type_index():
    store = make_store()
    assert store.of_type(CardType.MINION) == [10]
    store.apply(TagSet(10, GameTag.CARDTYPE, CardType.HERO))
    assert store.of_type(CardType.MINION) == []
    assert store.of_type(CardType.HERO) == [10]


def test_game_started_resets_everything():
    store = make_store()
    store.apply(GameStarted(100))
    assert store.game_entity_id == 100
    assert store.players == {}
    assert store.meta == {}
    assert store.card_id(10) is None
    assert store.in_zone(2, Zone.PLAY) == []
