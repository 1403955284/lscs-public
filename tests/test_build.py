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


def opponent(player_id, health, board, armor=0):
    from lscs.state.models import OpponentView

    return OpponentView(
        player_id=player_id,
        hero_card_id="HERO",
        health=health,
        armor=armor,
        place=player_id,
        tavern_tier=2,
        last_board=tuple(board),
        last_seen_turn=3,
    )


def test_opponent_alive_until_health_reaches_zero():
    assert opponent(2, 1, []).alive
    assert not opponent(3, 0, []).alive
    assert not opponent(4, -3, []).alive  # 淘汰后血量可以是负数


def test_held_minions_skip_eliminated_opponents():
    from factory import hand_minion, hand_spell, make_state, minion

    from lscs.state.build import held_minions

    mine = minion(10, "BG21_029", 1, 1)
    in_hand = hand_minion(11, "BGS_061", 3, 1)
    me = make_state(board=[mine], hand=[in_hand, hand_spell(12, "BG28_500")]).me
    alive = minion(20, "BG23_009", 2, 5)
    dead = minion(30, "BGS_116", 4, 5)
    negative = minion(40, "BG21_029_G", 2, 2, golden=True)
    opponents = {
        2: opponent(2, 12, [alive]),
        3: opponent(3, 0, [dead]),
        4: opponent(4, -3, [negative]),
    }
    assert held_minions(me, opponents) == [mine, in_hand, alive]
    assert held_minions(None, opponents) == [alive]


# ---- 战斗结果（提前显示战斗结果设计文档第 3.2 节）----

COMBAT_STARTS = "Entity=3 tag=HERO_ENTITY value=40"
SETTLES = "tag=STEP value=MAIN_CLEANUP"
OPPONENT_HERO = "TB_BaconShop_HERO_75"


def start_of_combat(mini_lines, *changes, opponent=3):
    from lscs.state.build import combat_start

    store = store_until(mini_lines, COMBAT_STARTS)
    for change in changes:
        store.apply(change)
    return combat_start(store, opponent, OPPONENT_HERO)


def settle(mini_lines, start, *changes):
    from lscs.state.build import combat_result

    store = store_until(mini_lines, SETTLES)
    for change in changes:
        store.apply(change)
    return combat_result(store, start)


def test_combat_start_and_result_in_the_mini_log(mini_lines):
    from lscs.state.build import CombatStart
    from lscs.state.models import CombatResult

    start = start_of_combat(mini_lines)
    assert start == CombatStart(
        raw_turn=2,
        turn=1,
        opponent_id=3,
        opponent_hero=OPPONENT_HERO,
        my_health=30,
        my_armor=0,
        opponent_health=30,
        opponent_armor=0,
        ghost=False,
    )
    assert settle(mini_lines, start) == CombatResult(
        turn=1,
        opponent_id=3,
        opponent_hero=OPPONENT_HERO,
        outcome="loss",
        ghost=False,
        lost=3,
        dealt=0,
        my_health=27,
        my_armor=0,
        opponent_health=30,
        opponent_armor=0,
        me_dead=False,
        opponent_dead=False,
    )


def test_damage_to_the_opponent_is_a_win_even_without_the_tag(mini_lines):
    from lscs.parsing.feed import TagSet

    start = start_of_combat(mini_lines, TagSet(12, GameTag.ARMOR, 4))
    assert (start.opponent_health, start.opponent_armor) == (30, 4)
    # 先扣护甲再扣血：30+4 → 27
    result = settle(mini_lines, start, TagSet(12, GameTag.DAMAGE, 3), TagSet(12, GameTag.ARMOR, 0))
    assert (result.outcome, result.dealt, result.lost) == ("win", 7, 3)
    assert (result.opponent_health, result.opponent_armor, result.opponent_dead) == (27, 0, False)


def test_armor_counts_and_healing_is_not_negative_damage(mini_lines):
    from lscs.parsing.feed import TagSet

    start = start_of_combat(mini_lines, TagSet(14, GameTag.ARMOR, 5))
    assert (start.my_health, start.my_armor) == (30, 5)
    armored = settle(mini_lines, start, TagSet(14, GameTag.ARMOR, 2))
    assert (armored.lost, armored.my_health, armored.my_armor) == (6, 27, 2)
    healed = settle(mini_lines, start, TagSet(14, GameTag.DAMAGE, 0), TagSet(14, GameTag.ARMOR, 9))
    assert (healed.lost, healed.outcome) == (0, "tie")


def test_tie_when_nobody_takes_damage(mini_lines):
    from lscs.parsing.feed import TagSet

    result = settle(mini_lines, start_of_combat(mini_lines), TagSet(14, GameTag.DAMAGE, 0))
    assert (result.outcome, result.lost, result.dealt) == ("tie", 0, 0)


def test_ghost_opponent(mini_lines):
    from lscs.parsing.feed import TagSet

    gone = TagSet(12, GameTag.DAMAGE, 30)  # 对手 3 开打前已经淘汰
    start = start_of_combat(mini_lines, gone)
    assert start.ghost and start.opponent_health == 0
    won = settle(
        mini_lines,
        start,
        gone,
        TagSet(14, GameTag.DAMAGE, 0),
        TagSet(2, GameTag.BACON_WON_LAST_COMBAT, 1),
    )
    assert (won.outcome, won.ghost, won.lost, won.dealt) == ("win", True, 0, 0)
    assert (won.opponent_health, won.opponent_dead) == (0, False)  # 残影不算"对手被淘汰"
    # 残影也能打赢我：按输处理，写掉血
    lost = settle(mini_lines, start, gone)
    assert (lost.outcome, lost.ghost, lost.lost) == ("loss", True, 3)


def test_eliminations(mini_lines):
    from lscs.parsing.feed import TagSet

    start = start_of_combat(mini_lines)
    killed = settle(
        mini_lines,
        start,
        TagSet(14, GameTag.DAMAGE, 0),
        TagSet(12, GameTag.DAMAGE, 38),
        TagSet(2, GameTag.BACON_WON_LAST_COMBAT, 1),
    )
    assert (killed.outcome, killed.dealt, killed.opponent_health) == ("win", 38, -8)
    assert killed.opponent_dead and not killed.me_dead
    dead = settle(mini_lines, start, TagSet(14, GameTag.DAMAGE, 44))
    assert (dead.outcome, dead.lost, dead.my_health) == ("loss", 44, -14)
    assert dead.me_dead and not dead.opponent_dead


def test_unknown_opponent_only_measures_my_side(mini_lines):
    from lscs.parsing.feed import TagSet

    start = start_of_combat(mini_lines, opponent=None)
    assert (start.opponent_id, start.opponent_hero, start.opponent_health) == (None, None, None)
    assert start.ghost is False
    result = settle(mini_lines, start, TagSet(12, GameTag.DAMAGE, 5))
    assert (result.outcome, result.lost, result.dealt) == ("loss", 3, 0)
    assert result.opponent_health is None and result.opponent_armor is None
    assert not result.opponent_dead
    # 对手编号认出来了但排行榜上没有这个人：一样当作不知道
    missing = start_of_combat(mini_lines, opponent=9)
    assert (missing.opponent_id, missing.opponent_hero, missing.opponent_health) == (9, None, None)
