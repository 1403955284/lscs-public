"""直接构造酒馆战棋局面，供建议相关的测试使用。"""

from lscs.state.models import (
    GameState,
    HandCard,
    HeroPower,
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


AUTO = object()


def power(
    card_id="BG21_HERO_000p",
    *,
    entity_id=5,
    cost=0,
    option="usable",
    used=False,
    reason=None,
    targets=(),
    needs_target=AUTO,
    passive=AUTO,
):
    """我方英雄技能；默认是凯瑞尔的"定罪"：0 费、现在能用、不需要目标。
    needs_target 默认看有没有 targets；passive 默认 option == "absent"。"""
    return HeroPower(
        entity_id=entity_id,
        card_id=card_id,
        cost=cost,
        used_this_turn=used,
        option=option,
        unusable_reason=reason,
        targets=tuple(targets),
        needs_target=bool(targets) if needs_target is AUTO else needs_target,
        passive=option == "absent" if passive is AUTO else passive,
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
    hero_power_state=None,
):
    return GameState(
        seq=seq,
        phase=phase,
        turn=turn,
        main_action=main_action,
        me=PlayerView(
            player_id=1,
            hero_card_id=hero,
            hero_power_card_id=hero_power_state.card_id if hero_power_state else hero_power,
            health=health,
            armor=armor,
            gold=gold,
            tavern_tier=tier,
            upgrade_cost=upgrade_cost,
            refresh_cost=refresh_cost,
            board=tuple(board),
            hand=tuple(hand),
            trinkets=tuple(trinkets),
            hero_power=hero_power_state,
        ),
        shop=tuple(shop),
        tribes=frozenset(tribes),
        tribes_confidence=TribesConfidence.CONFIRMED if tribes else TribesConfidence.INFERRING,
        global_dbfs=tuple(global_dbfs),
    )


# 用户录像里的四个备选英雄（实体编号、卡牌 ID、护甲、技能 dbfId），按位置排列
HEROES = (
    (89, "BG21_HERO_000", 10, 73941),
    (90, "BG28_HERO_800", 18, 105432),
    (91, "BG33_HERO_001", 19, 129164),
    (92, "TB_BaconShop_HERO_91", 17, 64486),
)


def hero_select_state(heroes=HEROES, *, seq=1, chosen=None, global_dbfs=(), uncertain=False):
    """选英雄阶段的局面。chosen 是已经选好的英雄卡牌 ID：这时手牌区没有英雄，hero_select 为 None。"""
    from lscs.state.models import HeroOption, HeroSelect

    state = make_state(
        turn=0,
        phase=Phase.HERO_SELECT,
        seq=seq,
        hero=chosen or "TB_BaconShop_HERO_PH",
        hero_power=None,
        upgrade_cost=None,
        refresh_cost=None,
        global_dbfs=global_dbfs,
        main_action=False,
    )
    select = None
    if chosen is None:
        select = HeroSelect(
            offered=tuple(
                HeroOption(
                    entity_id=e, card_id=c, position=i, armor=a, health=30, hero_power_dbf=dbf
                )
                for i, (e, c, a, dbf) in enumerate(heroes, start=1)
            ),
            reroll=True,
        )
    return state.model_copy(update={"hero_select": select, "uncertain": uncertain})


def offered_event(state, changed=0):
    from lscs.state.models import Event, EventType

    count = len(state.hero_select.offered)
    return Event(type=EventType.HEROES_OFFERED, turn=0, data={"count": count, "changed": changed})


def choice_option(
    entity_id,
    card_id,
    index,
    atk=None,
    health=None,
    *,
    card_type="MINION",
    tier=None,
    races=(),
    keywords=(),
    cost=None,
    gifts=(),
):
    from lscs.state.models import ChoiceOption

    return ChoiceOption(
        entity_id=entity_id,
        card_id=card_id,
        card_type=card_type,
        index=index,
        atk=atk,
        health=health,
        tier=tier,
        races=tuple(races),
        keywords=tuple(keywords),
        cost=cost,
        gifts=tuple(gifts),
    )


# 用户录像第 9 回合的黑暗之赐（id=12）：三个随从，属性已经算上了赐福
DARK_GIFT_OPTIONS = (
    (6931, "BG36_312", 6, 2, 4, ("ABERRATION",), "BG36_MidGameEffect_000t13"),
    (6932, "BG33_318", 1, 20, 5, ("MURLOC",), "BG36_MidGameEffect_000t2"),
    (6933, "BG36_356", 32, 32, 6, (), "BG36_MidGameEffect_000t29"),
)


def make_choice(options=None, *, choice_id=12, kind="dark_gift", source="BG36_MidGameEffect_010"):
    """一组"选一个"；默认是 DARK_GIFT_OPTIONS。options 可以是 ChoiceOption 的列表。"""
    from lscs.state.models import Choice, ChoiceKind

    if options is None:
        options = [
            choice_option(e, c, i, atk, health, tier=tier, races=races, gifts=(gift,))
            for i, (e, c, atk, health, tier, races, gift) in enumerate(DARK_GIFT_OPTIONS)
        ]
    return Choice(
        id=choice_id, kind=ChoiceKind(kind), source_card_id=source, options=tuple(options)
    )


def choice_state(choice=None, *, seq=1, turn=9, main_action=True, **kwargs):
    """招募阶段、游戏正弹着一组"选一个"的局面（默认是黑暗之赐）。"""
    state = make_state(turn=turn, seq=seq, main_action=main_action, **kwargs)
    return state.model_copy(update={"choice": choice if choice is not None else make_choice()})


def choice_event(state, kind=None):
    from lscs.state.models import Event, EventType

    choice = state.choice
    data = {"id": choice.id, "kind": kind or choice.kind.value, "count": len(choice.options)}
    return Event(type=EventType.CHOICE_OFFERED, turn=state.turn, data=data)


def resolved_event(choice_id, turn=9, picked=None, card=None):
    from lscs.state.models import Event, EventType

    data = {"id": choice_id, "picked": picked, "card": card}
    return Event(type=EventType.CHOICE_RESOLVED, turn=turn, data=data)
