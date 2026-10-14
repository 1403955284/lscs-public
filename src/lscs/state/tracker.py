"""维护实体状态，在 flush 时生成 GameState 快照和事件。"""

from __future__ import annotations

from collections.abc import Iterable

from hearthstone.enums import CardType, GameTag, Step, Zone

from lscs.parsing.feed import GameStarted, OptionsStarted, PowerChange, TagSet
from lscs.state.build import (
    BOB_HERO_PREFIX,
    HERO_PLACEHOLDER,
    CombatStart,
    bg_turn,
    bob_player_id,
    build_state,
    combat_result,
    combat_start,
    me_player_id,
    minion,
    opponents,
    raw_turn,
    shop_items,
    sorted_zone,
)
from lscs.state.entities import EntityStore
from lscs.state.models import CombatResult, Event, EventType, GameState, Minion, Phase
from lscs.state.pool import PoolConfig
from lscs.state.tribes import TribeInference


class StateTracker:
    def __init__(self, pool_config: PoolConfig) -> None:
        self._pool_config = pool_config
        self._store = EntityStore()
        self._seq = 0
        self._dirty = False
        self._reset_game()

    @property
    def state(self) -> GameState:
        return self._state

    @property
    def awaiting_options(self) -> bool:
        """招募刚开始（还没 flush），本回合的选项还没到。

        选项在 STEP 变成 MAIN_ACTION 之后约 0.1 秒、换了时间戳才写进日志；
        Pipeline 据此把选项并进招募开始的那次更新，开局的建议就知道英雄技能能不能用。"""
        return self._awaiting_options

    def _reset_game(self) -> None:
        self._state = GameState(seq=self._seq)
        self._tribes = TribeInference(self._pool_config.tribe_count)
        self._last_boards: dict[int, tuple[tuple[Minion, ...], int]] = {}
        self._pending: list[Event] = []
        self._in_combat = False
        self._combat_opponent: int | None = None
        # 这一场战斗开始时的双方血量；结算（STEP 变成 MAIN_CLEANUP / FINAL_WRAPUP）后清空
        self._combat_start: CombatStart | None = None
        self._last_combat: CombatResult | None = None  # 本局最近一场结算过的结果
        self._board_captured = True
        self._uncertain = False
        self._awaiting_options = False
        self._heroes_offered = False  # 本局已经发过备选英雄事件（再发就是重随）

    def mark_uncertain(self) -> None:
        self._uncertain = True
        self._dirty = True

    def apply(self, changes: Iterable[PowerChange]) -> None:
        for change in changes:
            if isinstance(change, GameStarted):
                self._reset_game()
            if isinstance(change, TagSet):
                self._before_tag(change)
            self._store.apply(change)
            if isinstance(change, TagSet):
                self._after_tag(change)
            elif isinstance(change, OptionsStarted):
                self._awaiting_options = False
            self._dirty = True

    def flush(self) -> tuple[GameState, list[Event]] | None:
        if not self._dirty:
            return None
        self._dirty = False
        self._awaiting_options = False
        for item in shop_items(self._store):
            self._tribes.observe(item.races)
        self._seq += 1
        new = build_state(
            self._store,
            seq=self._seq,
            last_boards=self._last_boards,
            tribes=self._tribes.tribes,
            tribes_confidence=self._tribes.confidence,
            pool_config=self._pool_config,
            uncertain=self._uncertain,
            last_combat=self._last_combat,
        )
        before, after = _diff_events(self._state, new, self._heroes_offered, self._picked())
        if any(e.type == EventType.HEROES_OFFERED for e in before):
            self._heroes_offered = True
        pending = self._pending if new.phase != Phase.WAITING else []  # 非酒馆战棋对局不发事件
        events = before + pending + after
        self._pending = []
        self._state = new
        return new, events

    def _picked(self) -> int | None:
        """上一个快照里那组"选一个"选中的实体（SendChoices 记下的；编号对得上才算）。"""
        old, current = self._state.choice, self._store.choice
        if old is None or current is None or current.id != old.id or not current.chosen:
            return None
        return current.chosen[0]

    def _before_tag(self, change: TagSet) -> None:
        # 第一次攻击之前截取对手阵容，这时还没有随从死亡
        if change.tag == GameTag.ATTACKING and change.value and not self._board_captured:
            self._capture_combat_board()

    def _after_tag(self, change: TagSet) -> None:
        store = self._store
        if change.entity_id == store.game_entity_id:
            if (
                change.tag == GameTag.STEP
                and change.value == Step.MAIN_ACTION
                and raw_turn(store) % 2 == 1
            ):
                self._pending.append(Event(type=EventType.RECRUIT_STARTED, turn=bg_turn(store)))
                self._awaiting_options = True
            start = self._combat_start
            if start is None:
                return
            if change.tag == GameTag.TURN and raw_turn(store) != start.raw_turn:
                self._combat_start = None  # 日志有缺口：没等到结算就换了回合，这一场没有结果
            elif (
                change.tag == GameTag.STEP
                and change.value in (Step.MAIN_CLEANUP, Step.FINAL_WRAPUP)
                and raw_turn(store) == start.raw_turn
            ):
                self._settle_combat(start)
            return
        if change.tag != GameTag.HERO_ENTITY:
            return
        hero_card = store.card_id(change.value)
        if change.entity_id == store.player_entity(me_player_id(store)):
            if raw_turn(store) == 0 and hero_card and hero_card != HERO_PLACEHOLDER:
                self._pending.append(
                    Event(type=EventType.HERO_CHOSEN, turn=0, data={"hero": hero_card})
                )
        elif change.entity_id == store.player_entity(bob_player_id(store)):
            if raw_turn(store) == 0 or hero_card is None:
                return  # 开局时 Bob 的英雄实体还没创建
            if hero_card.startswith(BOB_HERO_PREFIX):
                self._end_combat()
            else:
                self._start_combat(hero_card)

    def _start_combat(self, hero_card: str) -> None:
        self._in_combat = True
        self._combat_opponent = self._identify_opponent(hero_card)
        self._combat_start = combat_start(self._store, self._combat_opponent, hero_card)
        self._board_captured = False
        self._pending.append(
            Event(
                type=EventType.COMBAT_STARTED,
                turn=bg_turn(self._store),
                data={"opponent": self._combat_opponent, "hero": hero_card},
            )
        )

    def _end_combat(self) -> None:
        if not self._in_combat:
            return
        if not self._board_captured:
            self._capture_combat_board()
        self._pending.append(
            Event(
                type=EventType.COMBAT_ENDED,
                turn=bg_turn(self._store),
                data={"opponent": self._combat_opponent},
            )
        )
        self._in_combat = False
        self._combat_opponent = None

    def _settle_combat(self, start: CombatStart) -> None:
        """整场战斗在日志里已经写完（比画面动画早几十秒）：算出结果，每场只结算一次。"""
        result = combat_result(self._store, start)
        self._last_combat = result
        self._combat_start = None
        self._pending.append(
            Event(
                type=EventType.COMBAT_RESULT,
                turn=result.turn,
                data={
                    "opponent": result.opponent_id,
                    "outcome": result.outcome,
                    "lost": result.lost,
                    "dealt": result.dealt,
                },
            )
        )

    def _identify_opponent(self, hero_card: str) -> int | None:
        for player_id, opp in opponents(self._store, {}).items():
            if opp.hero_card_id == hero_card:
                return player_id
        me = self._store.player_entity(me_player_id(self._store))
        return self._store.tag(me, GameTag.NEXT_OPPONENT_PLAYER_ID) or None

    def _capture_combat_board(self) -> None:
        self._board_captured = True
        bob = bob_player_id(self._store)
        if self._combat_opponent is None or bob is None:
            return
        board = tuple(
            minion(self._store, e)
            for e in sorted_zone(self._store, bob, Zone.PLAY, (CardType.MINION,))
        )
        self._last_boards[self._combat_opponent] = (board, bg_turn(self._store))


def _diff_events(
    old: GameState, new: GameState, heroes_offered: bool = False, picked: int | None = None
) -> tuple[list[Event], list[Event]]:
    """heroes_offered：本局已经发过备选英雄事件，这次再发就是重随（changed=1）。
    picked：old 里那组"选一个"选中的实体（不知道时为 None）。"""
    before: list[Event] = []
    after: list[Event] = []
    if old.phase == Phase.WAITING and new.phase != Phase.WAITING:
        before.append(Event(type=EventType.GAME_STARTED, turn=new.turn))
    select = new.hero_select
    if new.phase == Phase.HERO_SELECT and select is not None:
        old_signature = old.hero_select.signature() if old.hero_select is not None else None
        if select.signature() != old_signature:
            before.append(
                Event(
                    type=EventType.HEROES_OFFERED,
                    turn=0,
                    data={"count": len(select.offered), "changed": int(heroes_offered)},
                )
            )
    if new.phase == Phase.RECRUIT and new.me is not None:
        old_me = old.me
        if old.shop != new.shop:
            after.append(Event(type=EventType.SHOP_CHANGED, turn=new.turn))
        if old_me is None or old_me.board != new.me.board:
            after.append(Event(type=EventType.BOARD_CHANGED, turn=new.turn))
        if old_me is None or old_me.hand != new.me.hand:
            after.append(Event(type=EventType.HAND_CHANGED, turn=new.turn))
        if old_me is None or old_me.gold != new.me.gold:
            after.append(Event(type=EventType.GOLD_CHANGED, turn=new.turn))
        if old_me is not None and old_me.hero_power != new.me.hero_power:
            after.append(Event(type=EventType.HERO_POWER_CHANGED, turn=new.turn))
    if new.phase == Phase.GAME_OVER and old.phase != Phase.GAME_OVER:
        after.append(
            Event(type=EventType.GAME_ENDED, turn=new.turn, data={"place": new.final_place})
        )
    # 选择事件放在最后：买卖带来的事件先处理（建议先算），再处理选项
    old_choice, new_choice = old.choice, new.choice
    if old_choice is not None and (new_choice is None or new_choice.id != old_choice.id):
        card = next((o.card_id for o in old_choice.options if o.entity_id == picked), None)
        data = {"id": old_choice.id, "picked": picked if card else None, "card": card}
        after.append(Event(type=EventType.CHOICE_RESOLVED, turn=new.turn or old.turn, data=data))
    if new_choice is not None and (old_choice is None or old_choice.id != new_choice.id):
        count = len(new_choice.options)
        data = {"id": new_choice.id, "kind": new_choice.kind.value, "count": count}
        after.append(Event(type=EventType.CHOICE_OFFERED, turn=new.turn, data=data))
    return before, after
