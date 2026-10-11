"""直接构造酒馆战棋局面，供建议相关的测试使用。"""

from lscs.state.models import (
    GameState,
    HandCard,
    Minion,
    Phase,
    PlayerView,
    ShopItem,
    TribesConfidence,
)


def minion(entity_id, card_id, atk, health, tier=1, races=(), keywords=(), golden=False):
    return Minion(
        entity_id=entity_id,
        card_id=card_id,
        position=0,
        tier=tier,
        races=tuple(races),
        atk=atk,
        health=health,
        golden=golden,
        keywords=tuple(keywords),
    )


def shop_minion(entity_id, card_id, atk, health, tier=1, races=(), keywords=(), golden=False):
    return ShopItem(
        entity_id=entity_id,
        card_id=card_id,
        card_type="MINION",
        position=0,
        tier=tier,
        races=tuple(races),
        atk=atk,
        health=health,
        golden=golden,
        frozen=False,
        keywords=tuple(keywords),
        cost=None,
    )


def shop_spell(entity_id, card_id, cost, tier=1):
    return ShopItem(
        entity_id=entity_id,
        card_id=card_id,
        card_type="BATTLEGROUND_SPELL",
        position=0,
        tier=tier,
        races=(),
        atk=None,
        health=None,
        golden=False,
        frozen=False,
        cost=cost,
    )


def hand_minion(entity_id, card_id, atk, health, tier=1, races=(), golden=False):
    return HandCard(
        entity_id=entity_id,
        card_id=card_id,
        card_type="MINION",
        tier=tier,
        atk=atk,
        health=health,
        golden=golden,
        races=tuple(races),
    )


def hand_spell(entity_id, card_id):
    return HandCard(
        entity_id=entity_id,
        card_id=card_id,
        card_type="BATTLEGROUND_SPELL",
        tier=1,
        atk=None,
        health=None,
        golden=False,
    )


def make_state(
    *,
    turn=1,
    gold=3,
    tier=1,
    health=30,
    armor=0,
    upgrade_cost=5,
    refresh_cost=1,
    shop=(),
    board=(),
    hand=(),
    hero="TB_BaconShop_HERO_49",
    hero_power="TB_BaconShop_HP_054",
    tribes=frozenset(),
    phase=Phase.RECRUIT,
    seq=1,
    trinkets=(),
    global_dbfs=(),
    main_action=True,
):
    return GameState(
        seq=seq,
        phase=phase,
        turn=turn,
        main_action=main_action,
        me=PlayerView(
            player_id=1,
            hero_card_id=hero,
            hero_power_card_id=hero_power,
            health=health,
            armor=armor,
            gold=gold,
            tavern_tier=tier,
            upgrade_cost=upgrade_cost,
            refresh_cost=refresh_cost,
            board=tuple(board),
            hand=tuple(hand),
            trinkets=tuple(trinkets),
        ),
        shop=tuple(shop),
        tribes=frozenset(tribes),
        tribes_confidence=TribesConfidence.CONFIRMED if tribes else TribesConfidence.INFERRING,
        global_dbfs=tuple(global_dbfs),
    )
