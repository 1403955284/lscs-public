"""游戏弹出的"选一个"：合成日志 choice.log（剧情见 fixtures/power/make_choice.py）。"""

import pytest
from helpers import FIXTURES, TEST_POOL

from lscs.pipeline import Pipeline
from lscs.replay import read_log_lines
from lscs.state.models import ChoiceKind, EventType, Phase
from lscs.state.tracker import StateTracker

CHOICE_LOG = FIXTURES / "power" / "choice.log"
CHOICE_EVENTS = (EventType.CHOICE_OFFERED, EventType.CHOICE_RESOLVED)


@pytest.fixture(scope="module")
def run():
    updates = []
    tracker = StateTracker(TEST_POOL)
    pipeline = Pipeline(tracker, lambda s, e: updates.append((s, e)))
    for line in read_log_lines(CHOICE_LOG):
        pipeline.push(line)
    pipeline.flush()
    return updates, tracker


def choice_events(updates):
    return [(e.type, e.data) for _, events in updates for e in events if e.type in CHOICE_EVENTS]


def offered(updates, choice_id):
    for state, events in updates:
        for e in events:
            if e.type == EventType.CHOICE_OFFERED and e.data["id"] == choice_id:
                return state
    raise AssertionError(f"没有选择 {choice_id}")


def test_event_sequence(run):
    updates, _ = run
    assert not any(state.uncertain for state, _ in updates)
    assert choice_events(updates) == [
        (EventType.CHOICE_OFFERED, {"id": 2, "kind": "hero_power", "count": 3}),
        (EventType.CHOICE_RESOLVED, {"id": 2, "picked": 61, "card": "BG21_HERO_000_Buddyt"}),
        (EventType.CHOICE_OFFERED, {"id": 3, "kind": "triple", "count": 3}),
        # 没选完就来了新的一组：旧的作废（不知道选了哪个），新的照常
        (EventType.CHOICE_RESOLVED, {"id": 3, "picked": None, "card": None}),
        (EventType.CHOICE_OFFERED, {"id": 4, "kind": "discover", "count": 3}),
        # 没有 SendChoices，但一个选项进了手牌：视为选完
        (EventType.CHOICE_RESOLVED, {"id": 4, "picked": None, "card": None}),
        # id=5 可以选两个、id=7 是 MULLIGAN：都不产生 choice
        (EventType.CHOICE_OFFERED, {"id": 6, "kind": "dark_gift", "count": 2}),
        (EventType.CHOICE_RESOLVED, {"id": 6, "picked": 82, "card": "BGS_061"}),
        (EventType.CHOICE_OFFERED, {"id": 8, "kind": "discover", "count": 2}),
    ]


def test_choice_before_main_action(run):
    updates, _ = run
    state = offered(updates, 2)
    assert (state.phase, state.turn, state.main_action) == (Phase.RECRUIT, 1, False)
    choice = state.choice
    assert (choice.id, choice.kind, choice.source_card_id) == (
        2,
        ChoiceKind.HERO_POWER,
        "BG21_HERO_000p",
    )
    assert [(o.entity_id, o.card_id, o.card_type, o.index) for o in choice.options] == [
        (60, "BG21_HERO_000p_t1", "SPELL", 0),
        (61, "BG21_HERO_000_Buddyt", "SPELL", 1),
        (62, "BG21_HERO_000_Buddyt2", "SPELL", 2),
    ]
    assert all(o.atk is None and o.health is None for o in choice.options)
    order = [e.type for _, events in updates for e in events]
    assert order.index(EventType.CHOICE_RESOLVED) < order.index(EventType.RECRUIT_STARTED)


def test_options_keep_the_order_of_the_log(run):
    updates, _ = run
    choice = offered(updates, 3).choice
    assert choice.kind == ChoiceKind.TRIPLE
    assert [(o.entity_id, o.index) for o in choice.options] == [(72, 0), (71, 1), (73, 2)]
    naga = choice.options[0]
    assert (naga.card_id, naga.atk, naga.health, naga.tier, naga.races, naga.keywords) == (
        "BG23_009",
        2,
        5,
        2,
        ("NAGA",),
        ("TAUNT",),
    )
    assert naga.gifts == ()


def test_dark_gift_options_carry_their_gifts(run):
    updates, _ = run
    choice = offered(updates, 6).choice
    assert (choice.kind, choice.source_card_id) == (ChoiceKind.DARK_GIFT, "BG36_MidGameEffect_010")
    first, second = choice.options
    # 附魔的创建者是赐福法术：记法术（说明更完整）；否则记附魔自己的卡牌 ID
    assert first.gifts == ("BG36_MidGameEffect_000t13",)
    assert first.keywords == ("DIVINE_SHIELD", "WINDFURY")
    assert second.gifts == ("BG36_MidGameEffect_000t2e2",)
    assert (second.atk, second.health) == (3, 11)


def test_choice_is_none_once_resolved_and_after_a_new_game(run):
    updates, tracker = run
    for state, events in updates:
        types = [e.type for e in events]
        if EventType.CHOICE_OFFERED in types:
            assert state.choice.id == events[-1].data["id"]  # 选项事件在最后
        elif EventType.CHOICE_RESOLVED in types:
            assert state.choice is None
    assert tracker.state.phase == Phase.WAITING
    assert tracker.state.choice is None
    assert tracker._store.choice is None  # CREATE_GAME 清空了没选完的那组


def store_at(marker):
    from lscs.parsing.feed import PowerFeed
    from lscs.state.entities import EntityStore

    feed, store = PowerFeed(), EntityStore()
    for line in read_log_lines(CHOICE_LOG):
        for change in feed.feed(line):
            store.apply(change)
        if marker in line:
            break
    return store


def test_choice_needs_my_options_and_at_least_two_of_them():
    from hearthstone.enums import GameTag

    from lscs.parsing.feed import ChoiceEntity, ChoiceOffered, ChoiceSource, TagSet
    from lscs.state.build import choice

    store = store_at("Entities[2]=[entityName=X id=73")
    assert choice(store).id == 3
    store.apply(TagSet(71, GameTag.CONTROLLER, 2))
    assert choice(store) is None  # 有一个选项不是我的
    for change in (ChoiceOffered(9, "GENERAL", 1), ChoiceSource(70), ChoiceEntity(72)):
        store.apply(change)
    assert choice(store) is None  # 只有一个选项


def test_choice_kind_of_a_trinket_offer():
    from lscs.parsing.feed import CardRevealed, ChoiceOffered, ChoiceSource
    from lscs.state.build import choice

    store = store_at("Entities[2]=[entityName=X id=73")
    store.apply(CardRevealed(70, "BG30_Trinket_2nd"))
    store.apply(ChoiceOffered(3, "GENERAL", 1))
    assert choice(store) is None  # 新的一组还没有选项
    store = store_at("Entities[2]=[entityName=X id=73")
    store.apply(CardRevealed(70, "BG30_Trinket_2nd"))
    store.apply(ChoiceSource(70))
    assert choice(store).kind == ChoiceKind.TRINKET


def test_an_unfinished_choice_does_not_come_back_next_turn():
    from hearthstone.enums import GameTag

    from lscs.parsing.feed import TagSet
    from lscs.state.build import choice

    store = store_at("Entities[2]=[entityName=X id=73")
    assert choice(store).id == 3
    store.apply(TagSet(store.game_entity_id, GameTag.TURN, 2))  # 进了战斗
    store.apply(TagSet(store.game_entity_id, GameTag.TURN, 3))  # 下一个招募回合，选项还在 SETASIDE
    assert choice(store) is None
