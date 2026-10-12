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
    assert [h.entity_id for h in state.hero_select.offered] == [14]  # 而是在 hero_select 里


def test_hero_select_only_while_choosing(mini_lines):
    chosen = build(store_until(mini_lines, "Entity=14 tag=ZONE value=PLAY"))
    assert chosen.phase == Phase.HERO_SELECT
    assert chosen.hero_select is None  # 选中的英雄离开了手牌区
    recruit = build(store_until(mini_lines, "tag=TURN value=1"))
    assert recruit.hero_select is None


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


def options(store, *listed):
    """在 store 上应用一组新的选项：listed 是 (kind, entity_id, error)。"""
    from lscs.parsing.feed import OptionListed, OptionsStarted

    store.apply(OptionsStarted())
    store.apply(OptionListed("option", None, "INVALID"))  # 结束回合
    for kind, entity_id, error in listed:
        store.apply(OptionListed(kind, entity_id, error))


def test_hero_power_unknown_until_this_turns_options_arrive(mini_lines):
    hp = build(store_until(mini_lines, FIRST_RECRUIT)).me.hero_power
    assert (hp.entity_id, hp.card_id, hp.cost, hp.used_this_turn) == (
        15,
        "TB_BaconShop_HP_054",
        0,
        False,
    )
    assert (hp.option, hp.unusable_reason, hp.targets, hp.needs_target, hp.passive) == (
        "unknown",
        None,
        (),
        None,  # 还没见过它能用的样子，不知道要不要目标
        False,
    )


def test_hero_power_usable_from_options(mini_lines):
    store = store_until(mini_lines, FIRST_RECRUIT)
    options(store, ("option", 15, None))
    hp = build(store).me.hero_power
    assert (hp.option, hp.unusable_reason, hp.needs_target, hp.passive) == (
        "usable",
        None,
        False,
        False,
    )


def test_hero_power_not_in_options_is_absent(mini_lines):
    store = store_until(mini_lines, FIRST_RECRUIT)
    options(store, ("option", 20, None))  # 只有刷新按钮
    hp = build(store).me.hero_power
    assert (hp.option, hp.passive) == ("absent", True)


def test_hero_power_missing_later_in_the_turn_is_not_called_passive(mini_lines):
    store = store_until(mini_lines, FIRST_RECRUIT)
    options(store, ("option", 15, None))  # 本回合第一组选项里有它
    options(store, ("option", 20, None))
    hp = build(store).me.hero_power
    assert (hp.option, hp.passive) == ("absent", False)


def test_hero_power_error_codes_make_it_unusable(mini_lines):
    for error in ("REQ_ENOUGH_MANA", "REQ_NOT_EXHAUSTED_HERO_POWER", "127"):
        store = store_until(mini_lines, FIRST_RECRUIT)
        options(store, ("option", 15, error))
        hp = build(store).me.hero_power
        assert (hp.option, hp.unusable_reason, hp.needs_target) == ("unusable", error, None)
    store = store_until(mini_lines, FIRST_RECRUIT)
    options(store, ("option", 15, "REQ_TARGET_TO_PLAY"))  # 这个错误码本身就说明要目标
    hp = build(store).me.hero_power
    assert (hp.option, hp.needs_target) == ("unusable", True)


def test_hero_power_targets_are_the_ones_without_error(mini_lines):
    store = store_until(mini_lines, FIRST_RECRUIT)
    options(
        store,
        ("option", 15, None),
        ("target", 30, None),
        ("target", 14, "REQ_MINION_TARGET"),
        ("target", 32, None),
    )
    hp = build(store).me.hero_power
    assert (hp.option, hp.needs_target, hp.targets) == ("usable", True, (30, 32))


OPTIONS = "D 10:00:11.0000000 GameState.DebugPrintOptions() - "
POWER_USABLE = [
    OPTIONS + "id=1",
    OPTIONS + "  option 0 type=END_TURN mainEntity= error=INVALID errorParam=",
    OPTIONS
    + "  option 1 type=POWER mainEntity=[entityName=法力风暴 id=15 zone=PLAY zonePos=0 cardId=TB_BaconShop_HP_054 player=1] error=NONE errorParam=",
]


def with_lines_after(lines, marker, extra):
    i = next(i for i, line in enumerate(lines) if marker in line)
    return lines[: i + 1] + extra + lines[i + 1 :]


def test_hero_power_options_from_log_lines_count_only_for_their_turn(mini_lines):
    lines = with_lines_after(mini_lines, FIRST_RECRUIT, POWER_USABLE)
    assert build(store_until(lines, "option 1 type=POWER")).me.hero_power.option == "usable"
    later = store_until(
        lines,
        "10:01:00.0000000 GameState.DebugPrintPower() - TAG_CHANGE Entity=GameEntity tag=STEP value=MAIN_ACTION",
    )
    assert build(later).me.hero_power.option == "unknown"  # 第 2 回合的选项还没到


def test_hero_power_cost_and_exhausted_tags(mini_lines):
    from lscs.parsing.feed import TagSet

    store = store_until(mini_lines, FIRST_RECRUIT)
    store.apply(TagSet(15, GameTag.COST, 2))
    store.apply(TagSet(15, GameTag.EXHAUSTED, 1))
    hp = build(store).me.hero_power
    assert (hp.cost, hp.used_this_turn) == (2, True)


def test_no_hero_power_entity(mini_lines):
    from hearthstone.enums import Zone

    from lscs.parsing.feed import TagSet

    store = store_until(mini_lines, FIRST_RECRUIT)
    store.apply(TagSet(15, GameTag.ZONE, Zone.GRAVEYARD))
    me = build(store).me
    assert (me.hero_power, me.hero_power_card_id) == (None, None)


AIMED = "[entityName=Aim id=15 zone=PLAY zonePos=0 cardId=TB_BaconShop_HP_054 player=1]"
IMP = "[entityName=Imp id=30 zone=PLAY zonePos=1 cardId=BG21_029 player=2]"


def test_paid_targeted_power_remembers_its_target_while_unaffordable(mini_lines):
    # 真实格式：铸币不够时选项没有目标行；能用的那一组才列出目标
    t = "D 10:00:11.0000000 GameState.DebugPrintOptions() - "
    u = "D 10:00:12.0000000 GameState.DebugPrintOptions() - "
    v = "D 10:00:13.0000000 GameState.DebugPrintOptions() - "
    lines = with_lines_after(
        mini_lines,
        FIRST_RECRUIT,
        [
            t + "id=1",
            t + f"  option 1 type=POWER mainEntity={AIMED} error=REQ_ENOUGH_MANA errorParam=",
            u + "id=2",
            u + f"  option 1 type=POWER mainEntity={AIMED} error=NONE errorParam=",
            u + f"    target 0 entity={IMP} error=NONE errorParam=",
            u + f"    target 1 entity={IMP} error=NONE errorParam=",
            v + "id=3",
            v + f"  option 1 type=POWER mainEntity={AIMED} error=REQ_ENOUGH_MANA errorParam=",
        ],
    )
    first = build(
        store_until(lines, "10:00:11.0000000 GameState.DebugPrintOptions() -   option")
    ).me
    assert (first.hero_power.option, first.hero_power.needs_target) == ("unusable", None)
    usable = build(store_until(lines, "    target 1 entity=")).me.hero_power
    assert (usable.option, usable.needs_target, usable.targets) == ("usable", True, (30,))
    later = build(store_until(lines, "10:00:13.0000000 GameState.DebugPrintOptions() -   option"))
    hp = later.me.hero_power
    assert (hp.option, hp.unusable_reason, hp.needs_target, hp.targets) == (
        "unusable",
        "REQ_ENOUGH_MANA",
        True,
        (),
    )
