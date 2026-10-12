from hearthstone.enums import CardType, GameTag, Zone

from lscs.parsing.feed import (
    CardRevealed,
    ChoiceChosen,
    ChoiceEntity,
    ChoiceOffered,
    ChoiceSent,
    ChoiceSource,
    GameMeta,
    GameStarted,
    OptionListed,
    OptionsStarted,
    PlayerDeclared,
    TagSet,
)
from lscs.state.entities import EntityStore, OpenChoice


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


def test_options_are_kept_per_main_entity_for_the_turn_they_arrived_in():
    store = make_store()
    store.apply(TagSet(1, GameTag.TURN, 3))
    for change in [
        OptionsStarted(),
        OptionListed("option", None, "INVALID"),
        OptionListed("option", 15, None),
        OptionListed("target", 10, None),
        OptionListed("target", 11, "REQ_MINION_TARGET"),
        OptionListed("option", 21, "REQ_ENOUGH_MANA"),
    ]:
        store.apply(change)
    assert store.options_turn == 3
    power = store.options[15]
    assert (power.error, power.valid_targets()) == (None, (10,))
    upgrade = store.options[21]
    assert (upgrade.error, upgrade.valid_targets()) == ("REQ_ENOUGH_MANA", ())
    assert None not in store.options
    assert store.needs_target == {15: True}  # 只记 error=NONE 列出的操作


def apply_group(store, *listed):
    store.apply(OptionsStarted())
    for change in listed:
        store.apply(OptionListed(*change))


def test_target_requirement_is_remembered_from_the_last_usable_listing():
    # 带错误码的选项从来没有目标行（真实日志里没有一个例外），所以要记住能用时的样子
    store = make_store()
    store.apply(TagSet(1, GameTag.TURN, 3))
    apply_group(store, ("option", 15, "REQ_ENOUGH_MANA"))
    assert store.needs_target.get(15) is None
    apply_group(store, ("option", 15, None), ("target", 10, None), ("target", 11, "127"))
    assert store.needs_target[15] is True
    apply_group(store, ("option", 15, "REQ_ENOUGH_MANA"))
    assert store.needs_target[15] is True
    store.apply(TagSet(1, GameTag.TURN, 5))
    apply_group(store, ("option", 15, None))
    assert store.needs_target[15] is False


def test_targets_are_deduped_and_choose_one_targets_must_suit_every_choice():
    store = make_store()
    apply_group(
        store,
        ("option", 15, None),
        ("target", 10, None),
        ("target", 10, None),
        ("subOption", 40, None),
        ("target", 11, None),
        ("target", 12, None),
        ("subOption", 41, None),
        ("target", 12, None),
        ("target", 13, None),
        ("option", 21, None),
        ("subOption", 42, None),
        ("target", 14, None),
        ("subOption", 43, None),  # 这一项不需要目标：没有对每一项都合法的目标
    )
    assert store.options[15].valid_targets() == (10, 12)
    assert store.options[21].valid_targets() == ()
    assert store.needs_target == {15: True, 21: True}


def test_first_option_group_of_each_turn_is_remembered():
    store = make_store()
    store.apply(TagSet(1, GameTag.TURN, 3))
    apply_group(store, ("option", 15, None))
    apply_group(store, ("option", 21, None))
    assert store.first_listed == {15}
    store.apply(TagSet(1, GameTag.TURN, 5))
    apply_group(store, ("option", 21, None))
    assert store.first_listed == {21}


def test_new_option_group_replaces_the_old_one():
    store = make_store()
    store.apply(TagSet(1, GameTag.TURN, 3))
    store.apply(OptionsStarted())
    store.apply(OptionListed("option", 15, None))
    store.apply(TagSet(1, GameTag.TURN, 5))
    store.apply(OptionsStarted())
    store.apply(OptionListed("option", 21, None))
    assert store.options_turn == 5
    assert set(store.options) == {21}


def test_targets_without_an_option_are_ignored():
    store = make_store()
    store.apply(OptionsStarted())
    store.apply(OptionListed("target", 10, None))
    assert store.options == {}


def test_game_started_clears_options():
    store = make_store()
    store.apply(OptionsStarted())
    store.apply(OptionListed("option", 15, None))
    store.apply(GameStarted(100))
    assert (store.options, store.options_turn) == ({}, None)
    assert (store.needs_target, store.first_listed) == ({}, set())


def test_choice_is_built_from_its_rows_and_marked_when_sent():
    store = make_store()
    for change in [
        ChoiceOffered(2, "GENERAL", 1),
        ChoiceSource(15),
        ChoiceEntity(62),
        ChoiceEntity(60),
    ]:
        store.apply(change)
    assert store.choice == OpenChoice(
        id=2, choice_type="GENERAL", max=1, source=15, entities=[62, 60]
    )
    store.apply(ChoiceSent(2, "GENERAL"))
    store.apply(ChoiceChosen(60))
    assert (store.choice.sent, store.choice.chosen) == (True, [60])


def test_new_choice_replaces_the_unfinished_one():
    store = make_store()
    store.apply(ChoiceOffered(3, "GENERAL", 1))
    store.apply(ChoiceEntity(70))
    store.apply(ChoiceOffered(4, "GENERAL", 1))
    store.apply(ChoiceEntity(80))
    assert (store.choice.id, store.choice.entities, store.choice.source) == (4, [80], None)


def test_choices_sent_for_another_id_are_ignored():
    store = make_store()
    store.apply(ChoiceOffered(4, "GENERAL", 1))
    store.apply(ChoiceEntity(80))
    store.apply(ChoiceSent(3, "GENERAL"))
    store.apply(ChoiceChosen(70))
    assert (store.choice.sent, store.choice.chosen) == (False, [])
    store.apply(ChoiceSent(4, "GENERAL"))
    store.apply(ChoiceChosen(80))
    assert (store.choice.sent, store.choice.chosen) == (True, [80])


def test_choice_rows_without_an_open_choice_are_ignored():
    store = make_store()
    for change in [ChoiceSource(15), ChoiceEntity(60), ChoiceSent(2, "GENERAL"), ChoiceChosen(60)]:
        store.apply(change)
    assert store.choice is None


def test_game_started_clears_the_choice():
    store = make_store()
    store.apply(ChoiceOffered(2, "GENERAL", 1))
    store.apply(GameStarted(100))
    assert store.choice is None
