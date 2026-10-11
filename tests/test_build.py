from hearthstone.enums import GameTag, Race
from helpers import TEST_POOL

from lscs.parsing.feed import PowerFeed
from lscs.state.build import build_state, races_of
from lscs.state.entities import EntityStore
from lscs.state.models import Phase, TribesConfidence


def store_until(lines, stop_after: str) -> EntityStore:
    """把日志喂到（含）第一行包含 stop_after 的位置为止。"""
    feed, store = PowerFeed(), EntityStore()
    for line in lines:
        for change in feed.feed(line):
            store.apply(change)
        if stop_after in line:
            break
    return store


def build(store, **overrides):
    kwargs = dict(
        seq=1,
        last_boards={},
        tribes=frozenset(),
        tribes_confidence=TribesConfidence.INFERRING,
        pool_config=TEST_POOL,
        uncertain=False,
    )
    kwargs.update(overrides)
    return build_state(store, **kwargs)


def test_hero_select_phase(mini_lines):
    state = build(store_until(mini_lines, "PlayerName=The Innkeeper"))
    assert state.phase == Phase.HERO_SELECT
    assert state.turn == 0
    assert state.me.hand == ()  # 备选英雄不算手牌
    assert set(state.opponents) == {3, 4}


def test_first_recruit_turn(mini_lines):
    state = build(
        store_until(
            mini_lines,
            "10:00:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
        )
    )
    assert state.phase == Phase.RECRUIT
    assert state.turn == 1
    me = state.me
    assert (me.hero_card_id, me.health, me.gold, me.tavern_tier) == (
        "TB_BaconShop_HERO_49",
        30,
        3,
        1,
    )
    assert (me.upgrade_cost, me.refresh_cost) == (5, 1)
    assert me.hero_power_card_id == "TB_BaconShop_HP_054"
    assert [s.card_id for s in state.shop] == ["BG21_029", "BGS_061", "BGS_116"]
    taunt = state.shop[2]
    assert taunt.races == ("ELEMENTAL",)
    assert (taunt.atk, taunt.health, taunt.tier, taunt.frozen) == (1, 4, 1, False)
    assert state.next_opponent_id == 3
    assert state.pool_estimate == {"BG21_029": 16, "BGS_061": 16, "BGS_116": 16}


def test_buy_moves_minion_to_hand_and_spends_gold(mini_lines):
    state = build(store_until(mini_lines, "Entity=31 tag=ZONE_POSITION value=1"))
    assert state.me.gold == 0
    assert [h.card_id for h in state.me.hand] == ["BGS_061"]
    assert [s.card_id for s in state.shop] == ["BG21_029", "BGS_116"]
    assert state.pool_estimate["BGS_061"] == 15


def test_frozen_shop(mini_lines):
    state = build(store_until(mini_lines, "Entity=32 tag=FROZEN value=1"))
    assert [s.frozen for s in state.shop] == [True, True]
    assert [(m.card_id, m.atk, m.health) for m in state.me.board] == [("BGS_061", 3, 1)]


def test_combat_phase_has_no_shop(mini_lines):
    state = build(store_until(mini_lines, "Entity=3 tag=HERO_ENTITY value=40"))
    assert state.phase == Phase.COMBAT
    assert state.shop == ()


def test_second_recruit_includes_tavern_spell_and_damage(mini_lines):
    state = build(
        store_until(
            mini_lines,
            "10:01:00.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
        )
    )
    assert state.turn == 2
    assert (state.me.health, state.me.gold, state.me.upgrade_cost) == (27, 4, 4)
    assert [(s.card_id, s.card_type) for s in state.shop] == [
        ("BG23_009", "MINION"),
        ("BG28_500", "BATTLEGROUND_SPELL"),
    ]
    assert "BG28_500" not in state.pool_estimate  # 法术不计入随从池


def test_game_over_has_final_place(mini_lines):
    state = build(store_until(mini_lines, "tag=STATE value=COMPLETE"))
    assert state.phase == Phase.GAME_OVER
    assert state.final_place == 5


def test_non_battlegrounds_and_duos_are_waiting(mini_lines):
    for game_type in ("GT_RANKED", "GT_BATTLEGROUNDS_DUO"):
        lines = [line.replace("GT_BATTLEGROUNDS", game_type) for line in mini_lines]
        state = build(
            store_until(
                lines,
                "10:00:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
            )
        )
        assert state.phase == Phase.WAITING
        assert state.me is None


def test_races_of_reads_cardrace_and_extra_race_tags():
    store = EntityStore()
    from lscs.parsing.feed import TagSet

    store.apply(TagSet(10, GameTag.CARDRACE, Race.MECHANICAL))
    store.apply(TagSet(10, Race.DEMON.race_tag, 1))  # 第二种族用独立的 tag 表示
    assert races_of(store, 10) == ("DEMON", "MECHANICAL")


FIRST_RECRUIT = "10:00:10.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION"


def test_shop_items_carry_keywords_and_cost(mini_lines):
    from hearthstone.enums import GameTag

    from lscs.parsing.feed import TagSet

    store = store_until(mini_lines, FIRST_RECRUIT)
    store.apply(TagSet(31, GameTag.COST, 2))
    state = build(store)
    assert [s.keywords for s in state.shop] == [(), (), ("TAUNT",)]
    assert [s.cost for s in state.shop] == [None, 2, None]


def test_hand_cards_carry_races(mini_lines):
    state = build(store_until(mini_lines, "Entity=31 tag=ZONE_POSITION value=1"))
    assert [(h.card_id, h.races) for h in state.me.hand] == [("BGS_061", ("PIRATE",))]


def test_trinkets_and_global_mechanics(mini_lines):
    from hearthstone.enums import CardType, GameTag, Zone

    from lscs.parsing.feed import CardRevealed, TagSet

    store = store_until(mini_lines, FIRST_RECRUIT)
    for entity_id, card_id, controller in [
        (901, "BG36_MagicItem_416", 1),
        (900, "BG36_MagicItem_402", 1),
        (902, "BG30_MagicItem_703", 2),  # 不是我的
    ]:
        store.apply(CardRevealed(entity_id, card_id))
        store.apply(TagSet(entity_id, GameTag.CONTROLLER, controller))
        store.apply(TagSet(entity_id, GameTag.CARDTYPE, CardType.BATTLEGROUND_TRINKET))
        store.apply(TagSet(entity_id, GameTag.ZONE, Zone.PLAY))
    for tag, value in [(GameTag.CONTROLLER, 1), (GameTag.CARDTYPE, CardType.BATTLEGROUND_TRINKET)]:
        store.apply(TagSet(903, tag, value))  # 卡牌 ID 还没公开的饰品
    store.apply(TagSet(903, GameTag.ZONE, Zone.PLAY))
    store.apply(TagSet(1, GameTag.BACON_GLOBAL_OLD_GOD_DBID, 132532))
    store.apply(TagSet(1, GameTag.BACON_GLOBAL_ANOMALY_DBID, 0))  # 0 表示没有
    state = build(store)
    assert state.me.trinkets == ("BG36_MagicItem_402", "BG36_MagicItem_416")
    assert state.global_dbfs == (132532,)


def test_main_action_follows_game_step(mini_lines):
    before = build(store_until(mini_lines, "Entity=2 tag=RESOURCES value=3"))
    assert (before.phase, before.main_action) == (Phase.RECRUIT, False)  # 铸币到了，还没开始
    assert build(store_until(mini_lines, FIRST_RECRUIT)).main_action is True
    ended = build(store_until(mini_lines, "tag=STEP value=MAIN_END"))
    assert ended.main_action is False
