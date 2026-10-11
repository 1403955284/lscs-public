"""从实体存储翻译出酒馆战棋语义。只依赖实体 tag，不依赖卡牌数据库。

规律来自真实日志（tests/fixtures/power/bg_139963.log.gz）：
- 游戏 TURN 为奇数是招募阶段，偶数是战斗阶段；酒馆回合 = (TURN + 1) // 2
- 商店 = 招募阶段 Bob（AI 玩家）场上的随从和酒馆法术
- 升本/刷新按钮是我方场上的 GAME_MODE_BUTTON 实体，COST 就是实际费用
- 商店里每张牌的 COST 都会读进 ShopItem.cost，但只有酒馆法术的 COST 是购买价格；
  随从的 COST 不是价格（随从按买牌价收费，见 advisor.context.TurnContext.price）
- 排行榜上的对手英雄由 Bob 控制，PLAYER_ID 是对手的玩家 ID
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping

from hearthstone.enums import CardType, GameTag, PlayState, Race, State, Step, Zone

from lscs.state.entities import EntityStore
from lscs.state.models import (
    GameState,
    HandCard,
    Minion,
    OpponentView,
    Phase,
    PlayerView,
    ShopItem,
    TribesConfidence,
)
from lscs.state.pool import PoolConfig, estimate_pool

BOB_HERO_PREFIX = "TB_BaconShopBob"
HERO_PLACEHOLDER = "TB_BaconShop_HERO_PH"
UPGRADE_BUTTON_PREFIX = "TB_BaconShopTechUp"
REROLL_BUTTON_MARK = "Reroll_Button"
SHOP_TYPES = (CardType.MINION, CardType.BATTLEGROUND_SPELL)
GAME_OVER_PLAYSTATES = (PlayState.WON, PlayState.LOST, PlayState.TIED, PlayState.CONCEDED)
KEYWORD_TAGS = {
    GameTag.TAUNT: "TAUNT",
    GameTag.DIVINE_SHIELD: "DIVINE_SHIELD",
    GameTag.REBORN: "REBORN",
    GameTag.POISONOUS: "POISONOUS",
    GameTag.VENOMOUS: "VENOMOUS",
    GameTag.WINDFURY: "WINDFURY",
    GameTag.STEALTH: "STEALTH",
}

# 饰品还没选时，我方场上是两个占位实体（"小型饰品"/"大型饰品"），选完后才变成真正的饰品
TRINKET_PLACEHOLDERS = frozenset({"BG30_Trinket_1st", "BG30_Trinket_2nd"})
GLOBAL_DBF_TAG = re.compile(r"BACON_GLOBAL_\w+_DBID")

LastBoards = Mapping[int, tuple[tuple[Minion, ...], int]]  # player_id -> (阵容, 酒馆回合)


def is_battlegrounds(store: EntityStore) -> bool:
    game_type = store.meta.get("GameType")
    if game_type is not None:
        return game_type.startswith("GT_BATTLEGROUNDS") and "DUO" not in game_type
    return any(
        (store.card_id(e) or "").startswith(BOB_HERO_PREFIX) for e in store.of_type(CardType.HERO)
    )


def me_player_id(store: EntityStore) -> int | None:
    return next((p for p in sorted(store.players) if p not in store.ai_players), None)


def bob_player_id(store: EntityStore) -> int | None:
    return next(iter(sorted(store.ai_players)), None)


def raw_turn(store: EntityStore) -> int:
    return store.tag(store.game_entity_id, GameTag.TURN, 0) or 0


def bg_turn(store: EntityStore) -> int:
    return (raw_turn(store) + 1) // 2


def is_main_action(store: EntityStore) -> bool:
    return store.tag(store.game_entity_id, GameTag.STEP) == Step.MAIN_ACTION


def phase_of(store: EntityStore) -> Phase:
    if store.game_entity_id is None or not is_battlegrounds(store):
        return Phase.WAITING
    if store.tag(store.game_entity_id, GameTag.STATE) == State.COMPLETE:
        return Phase.GAME_OVER
    me = store.player_entity(me_player_id(store))
    if store.tag(me, GameTag.PLAYSTATE) in GAME_OVER_PLAYSTATES:
        return Phase.GAME_OVER
    turn = raw_turn(store)
    if turn == 0:
        return Phase.HERO_SELECT
    return Phase.RECRUIT if turn % 2 == 1 else Phase.COMBAT


def races_of(store: EntityStore, entity_id: int) -> tuple[str, ...]:
    found: set[str] = set()
    for tag, value in store.tags_of(entity_id).items():
        if tag == GameTag.CARDRACE:
            race = value
        else:
            race = Race.get_race_for_game_tag(tag)
            if race is None or not value:
                continue
        try:
            found.add(Race(race).name)
        except ValueError:
            continue
    found.discard("INVALID")
    return tuple(sorted(found))


def _card_type_name(store: EntityStore, entity_id: int) -> str:
    value = store.tag(entity_id, GameTag.CARDTYPE, 0)
    try:
        return CardType(value).name
    except ValueError:
        return str(value)


def _tier(store: EntityStore, entity_id: int) -> int | None:
    return store.tag(entity_id, GameTag.TECH_LEVEL) or None


def _golden(store: EntityStore, entity_id: int) -> bool:
    return bool(store.tag(entity_id, GameTag.PREMIUM, 0))


def _current_health(store: EntityStore, entity_id: int) -> int:
    health = store.tag(entity_id, GameTag.HEALTH, 0) or 0
    return health - (store.tag(entity_id, GameTag.DAMAGE, 0) or 0)


def sorted_zone(
    store: EntityStore, controller: int, zone: int, types: Collection[int] | None = None
) -> list[int]:
    ids = [
        e
        for e in store.in_zone(controller, zone)
        if types is None or store.tag(e, GameTag.CARDTYPE) in types
    ]
    return sorted(ids, key=lambda e: (store.tag(e, GameTag.ZONE_POSITION, 0) or 0, e))


def _keywords(store: EntityStore, entity_id: int) -> tuple[str, ...]:
    return tuple(sorted(name for tag, name in KEYWORD_TAGS.items() if store.tag(entity_id, tag, 0)))


def minion(store: EntityStore, entity_id: int) -> Minion:
    return Minion(
        entity_id=entity_id,
        card_id=store.card_id(entity_id) or "",
        position=store.tag(entity_id, GameTag.ZONE_POSITION, 0) or 0,
        tier=_tier(store, entity_id),
        races=races_of(store, entity_id),
        atk=store.tag(entity_id, GameTag.ATK, 0) or 0,
        health=_current_health(store, entity_id),
        golden=_golden(store, entity_id),
        keywords=_keywords(store, entity_id),
    )


def shop_items(store: EntityStore) -> tuple[ShopItem, ...]:
    bob = bob_player_id(store)
    if bob is None or phase_of(store) != Phase.RECRUIT:
        return ()
    return tuple(
        ShopItem(
            entity_id=e,
            card_id=store.card_id(e) or "",
            card_type=_card_type_name(store, e),
            position=store.tag(e, GameTag.ZONE_POSITION, 0) or 0,
            tier=_tier(store, e),
            races=races_of(store, e),
            atk=store.tag(e, GameTag.ATK),
            health=store.tag(e, GameTag.HEALTH),
            golden=_golden(store, e),
            frozen=bool(store.tag(e, GameTag.FROZEN, 0)),
            keywords=_keywords(store, e),
            cost=store.tag(e, GameTag.COST),
        )
        for e in sorted_zone(store, bob, Zone.PLAY, SHOP_TYPES)
    )


def _button_cost(store: EntityStore, controller: int, matches) -> int | None:
    ids = [e for e in store.in_zone(controller, Zone.PLAY) if matches(store.card_id(e) or "")]
    if not ids:
        return None
    return store.tag(max(ids), GameTag.COST, 0)


def _hero_power(store: EntityStore, controller: int) -> str | None:
    ids = sorted_zone(store, controller, Zone.PLAY, (CardType.HERO_POWER,))
    return store.card_id(max(ids)) if ids else None


def _trinkets(store: EntityStore, controller: int) -> tuple[str, ...]:
    ids = sorted(
        e
        for e in store.in_zone(controller, Zone.PLAY)
        if store.tag(e, GameTag.CARDTYPE) == CardType.BATTLEGROUND_TRINKET
    )
    cards = (store.card_id(e) for e in ids)
    # 卡牌 ID 还没公开（空）的不算；占位实体也不算
    return tuple(c for c in cards if c and c not in TRINKET_PLACEHOLDERS)


def global_dbfs(store: EntityStore) -> tuple[int, ...]:
    """游戏实体上 BACON_GLOBAL_*_DBID 标签（本局古神、畸变等）的值，按标签名排序。"""
    if store.game_entity_id is None:
        return ()
    found: list[tuple[str, int]] = []
    for tag, value in store.tags_of(store.game_entity_id).items():
        try:
            name = GameTag(tag).name
        except ValueError:
            continue
        if value and GLOBAL_DBF_TAG.fullmatch(name):
            found.append((name, value))
    return tuple(value for _, value in sorted(found))


def player_view(store: EntityStore) -> PlayerView | None:
    player_id = me_player_id(store)
    entity = store.player_entity(player_id)
    if player_id is None or entity is None:
        return None
    hero = store.tag(entity, GameTag.HERO_ENTITY)
    gold = (
        (store.tag(entity, GameTag.RESOURCES, 0) or 0)
        + (store.tag(entity, GameTag.TEMP_RESOURCES, 0) or 0)
        - (store.tag(entity, GameTag.RESOURCES_USED, 0) or 0)
    )
    return PlayerView(
        player_id=player_id,
        hero_card_id=store.card_id(hero),
        hero_power_card_id=_hero_power(store, player_id),
        health=_current_health(store, hero) if hero else 0,
        armor=store.tag(hero, GameTag.ARMOR, 0) or 0,
        gold=max(gold, 0),
        tavern_tier=store.tag(entity, GameTag.PLAYER_TECH_LEVEL, 1) or 1,
        upgrade_cost=_button_cost(store, player_id, lambda c: c.startswith(UPGRADE_BUTTON_PREFIX)),
        refresh_cost=_button_cost(store, player_id, lambda c: REROLL_BUTTON_MARK in c),
        board=tuple(
            minion(store, e) for e in sorted_zone(store, player_id, Zone.PLAY, (CardType.MINION,))
        ),
        hand=tuple(
            HandCard(
                entity_id=e,
                card_id=store.card_id(e) or "",
                card_type=_card_type_name(store, e),
                tier=_tier(store, e),
                atk=store.tag(e, GameTag.ATK),
                health=store.tag(e, GameTag.HEALTH),
                golden=_golden(store, e),
                races=races_of(store, e),
            )
            for e in sorted_zone(store, player_id, Zone.HAND)
            if store.tag(e, GameTag.CARDTYPE) != CardType.HERO  # 选英雄阶段的备选英雄也在手牌区
        ),
        trinkets=_trinkets(store, player_id),
    )


def opponents(store: EntityStore, last_boards: LastBoards) -> dict[int, OpponentView]:
    me, bob = me_player_id(store), bob_player_id(store)
    latest: dict[int, int] = {}
    for e in store.of_type(CardType.HERO):
        player_id = store.tag(e, GameTag.PLAYER_ID)
        if (
            store.tag(e, GameTag.CONTROLLER) != bob
            or player_id in (None, me)
            or store.tag(e, GameTag.PLAYER_LEADERBOARD_PLACE) is None
        ):
            continue
        latest[player_id] = max(e, latest.get(player_id, e))
    result: dict[int, OpponentView] = {}
    for player_id, e in sorted(latest.items()):
        board, seen_turn = last_boards.get(player_id, ((), None))
        result[player_id] = OpponentView(
            player_id=player_id,
            hero_card_id=store.card_id(e),
            health=_current_health(store, e),
            armor=store.tag(e, GameTag.ARMOR, 0) or 0,
            place=store.tag(e, GameTag.PLAYER_LEADERBOARD_PLACE),
            tavern_tier=store.tag(e, GameTag.PLAYER_TECH_LEVEL),
            last_board=board,
            last_seen_turn=seen_turn,
        )
    return result


def build_state(
    store: EntityStore,
    *,
    seq: int,
    last_boards: LastBoards,
    tribes: frozenset[str],
    tribes_confidence: TribesConfidence,
    pool_config: PoolConfig,
    uncertain: bool,
) -> GameState:
    phase = phase_of(store)
    if phase == Phase.WAITING:
        return GameState(seq=seq, uncertain=uncertain)
    me = player_view(store)
    shop = shop_items(store)
    opps = opponents(store, last_boards)
    me_entity = store.player_entity(me_player_id(store))
    held = []
    if me is not None:
        held.extend(me.board)
        held.extend(card for card in me.hand if card.card_type == CardType.MINION.name)
    for opp in opps.values():
        held.extend(opp.last_board)
    final_place = None
    if phase == Phase.GAME_OVER and me_entity is not None:
        final_place = store.tag(
            store.tag(me_entity, GameTag.HERO_ENTITY), GameTag.PLAYER_LEADERBOARD_PLACE
        )
    return GameState(
        seq=seq,
        phase=phase,
        turn=bg_turn(store),
        main_action=is_main_action(store),
        me=me,
        shop=shop,
        opponents=opps,
        next_opponent_id=store.tag(me_entity, GameTag.NEXT_OPPONENT_PLAYER_ID) or None,
        tribes=tribes,
        tribes_confidence=tribes_confidence,
        pool_estimate=estimate_pool(
            pool_config.copies,
            held=held,
            seen=[item for item in shop if item.card_type == CardType.MINION.name],
        ),
        uncertain=uncertain,
        final_place=final_place,
        global_dbfs=global_dbfs(store),
    )
