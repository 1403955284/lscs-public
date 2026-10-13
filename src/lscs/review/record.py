"""对局记录：从相邻状态快照的差异里提取玩家每回合的操作和战斗结果（对局复盘设计文档第 3 节），
以及每回合的建议做了多少（第 5 节）。

只看 GameState，不靠 BLOCK_START（包的类型跨版本不一样）、不查卡牌库：卡名在输出时才翻译。
规律（两份真实日志逐回合核对过）：
- 升本：酒馆等级变高；刷新：两个商店都非空且实体编号完全不重合，或者买空后又出现商品；
- 买：商店里的实体出现在手牌或场上；买第三张时直接合成的，商店里那张凭空消失、铸币减少了
  至少一个买牌价、手牌里出现新的金色随从；
- 卖：场上的随从消失、没回到手牌、同一次更新里铸币增加，而且不是三连合成吃掉的；
- 三连：手牌或场上出现之前没有的金色随从；本次或相邻一次更新里消失的同名普通卡不算卖；
- 冻结：商店非空且全部冻结，上一个快照不是；英雄技能：从没用过变成用过（EXHAUSTED 或错误码）。
手牌里的随从没打出就消失（被战吼吃掉、被转化）、场上随从消失但没给钱，都不算操作。

战斗结果在下一回合招募开始（或对局结束）时结算：对手排行榜上的血量有时在战斗结束时还没更新。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lscs.advisor.context import MINION, hero_power_used, is_golden
from lscs.advisor.models import Advice, Plan
from lscs.state.models import Event, EventType, GameState, Minion, Phase, ShopItem
from lscs.state.pool import base_card_id

BuyCost = Callable[[str | None], int]  # 英雄卡牌 ID -> 买牌价
NormalId = Callable[[str], str]  # 金色卡 -> 普通卡 ID
OpKind = Literal["buy", "sell", "upgrade", "hero_power", "refresh", "freeze"]
MERGE_COPIES = 2  # 一次三连合成吃掉的普通卡（另一张是刚买或刚打出的）


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ChoicePick(_Frozen):
    id: int
    kind: str  # ChoiceKind.value
    card_id: str | None  # 玩家选的；不知道时 None
    cost: int | None  # 饰品价格
    ai_card_id: str | None = None  # AI 推荐的（有选项建议时）


class TurnAdvice(_Frozen):
    rules_upgrade: bool  # 回合开始的规则建议里有升本
    deep_summary: str  # 深度分析的 summary（没有为空）
    deep_direction: str | None
    deep_upgrade: bool
    fast_upgrade: bool = False  # 某条快速建议里有升本
    # 回合开始的规则建议：(动作, 买卖的卡牌 ID)
    rules_actions: tuple[tuple[str, str | None], ...] = ()
    advised: int  # 建议过的动作数（去重）
    followed: int  # 其中做了的
    missed_buys: tuple[str, ...]  # 建议买而没买的卡牌 ID

    @property
    def upgrade_advised(self) -> bool:
        """本回合的建议里有升本：深度分析、某条快速建议或回合开始的规则建议。"""
        return self.deep_upgrade or self.fast_upgrade or self.rules_upgrade


class TurnRecord(_Frozen):
    turn: int
    partial: bool = False  # 中途启动，没看到招募开始
    tier_start: int
    tier_end: int
    gold_start: int  # 招募开始时（饰品、姿态已经扣过）
    gold_end: int  # 进战斗前剩下的
    gold_gained: int  # 回合内铸币增加的总和（卖牌、法术）
    upgrade_gold: int = 0  # 升本花掉的铸币
    buys: tuple[str, ...]  # 买到的随从卡牌 ID（含合成三连的那张）
    spells: tuple[str, ...]  # 买的酒馆法术
    sells: tuple[str, ...]
    plays: tuple[str, ...]  # 从手牌打到场上的随从
    rerolls: int
    reroll_gold: int  # 刷新花掉的铸币
    paid_rerolls: int = 0  # 花了铸币的刷新（英雄技能、法术给的免费刷新不算）
    froze: bool  # 回合内冻结过（进战斗时商店还冻着也算）
    hero_power: int  # 用了几次
    hero_power_gold: int
    triples: tuple[str, ...]  # 新出现的金色随从的普通卡 ID
    choices: tuple[ChoicePick, ...]
    board_start: tuple[Minion, ...]
    board_end: tuple[Minion, ...]  # COMBAT_STARTED 那个快照的 me.board
    hand_end: int  # 进战斗时手牌里还有几张随从没打
    health_start: int  # 有效血量（血量 + 护甲）
    advice: TurnAdvice | None = None

    @property
    def upgraded(self) -> bool:
        return self.tier_end > self.tier_start


class CombatRecord(_Frozen):
    turn: int
    opponent_id: int | None
    opponent_hero: str | None  # 卡牌 ID
    opponent_place: int | None
    opponent_tier: int | None
    opponent_minions: int  # last_board 的个数（0 = 没截到）
    opponent_stats: int  # 攻 + 血 之和
    my_minions: int
    my_stats: int
    lost: int  # 我方掉的有效血量
    dealt: int  # 打出的（对手排行榜血量之差；残影为 0）
    ghost: bool  # 对手开打前已淘汰
    result: Literal["win", "loss", "tie", "ghost"]
    # win：dealt > 0；loss：lost > 0；ghost：对手是残影且没掉血；其余 tie
    health_after: int  # 结算时我方的有效血量


class GameRecord(_Frozen):
    hero_card_id: str | None
    hero_ai_pick: str | None = None  # 选英雄时 AI 推荐的卡牌 ID
    place: int | None
    tribes: tuple[str, ...]
    turns: tuple[TurnRecord, ...]
    combats: tuple[CombatRecord, ...]
    final_health: int  # GAME_ENDED 时的有效血量（淘汰时 ≤ 0）
    uncertain: bool
    seen_cards: frozenset[str]  # 本局出现过的所有卡牌 ID（商店、场面、手牌、选项、对手阵容）

    def turn(self, number: int) -> TurnRecord | None:
        return next((t for t in self.turns if t.turn == number), None)

    @property
    def last_turn(self) -> int:
        """打到第几回合（中途开始记录时前面的回合不在 turns 里，但也打过）。"""
        return self.turns[-1].turn if self.turns else 0

    @property
    def recorded_from(self) -> int | None:
        """中途开始记录（重连、中途启动）时从第几回合起有记录；从头记录时 None。"""
        return self.turns[0].turn if self.turns and self.turns[0].partial else None

    def combat(self, number: int) -> CombatRecord | None:
        return next((c for c in self.combats if c.turn == number), None)


@dataclass(frozen=True)
class Op:
    """状态差异里认出的一次操作（和建议比较用）。seq 是认出它的那个快照。"""

    turn: int
    seq: int
    kind: OpKind
    entity_id: int | None = None


def effective_health(state: GameState) -> int:
    me = state.me
    return me.health + me.armor if me is not None else 0


def stats(board: Iterable[Minion]) -> int:
    return sum(m.atk + m.health for m in board)


@dataclass
class _Turn:
    turn: int
    partial: bool
    tier_start: int
    gold_start: int
    health_start: int
    board_start: tuple[Minion, ...]
    last: GameState
    tier_end: int = 0
    gold_end: int = 0
    gold_gained: int = 0
    upgrade_gold: int = 0
    buys: list[str] = field(default_factory=list)
    spells: list[str] = field(default_factory=list)
    sells: list[str] = field(default_factory=list)
    plays: list[str] = field(default_factory=list)
    rerolls: int = 0
    reroll_gold: int = 0
    paid_rerolls: int = 0
    froze: bool = False
    hero_power: int = 0
    hero_power_gold: int = 0
    triples: list[str] = field(default_factory=list)
    board_end: tuple[Minion, ...] | None = None
    closed: bool = False


@dataclass
class _Choice:
    id: int
    turn: int
    kind: str
    options: dict[int, tuple[str, int | None]]  # 实体编号 -> (卡牌 ID, 价格)
    card_id: str | None = None
    entity_id: int | None = None


@dataclass
class _Combat:
    turn: int
    opponent_id: int | None
    opponent_hero: str | None
    opponent_place: int | None
    opponent_tier: int | None
    opponent_health: int | None  # 开打前的有效血量
    ghost: bool
    my_health: int
    my_minions: int
    my_stats: int
    opponent_minions: int = 0
    opponent_stats: int = 0


@dataclass(frozen=True)
class _HeldSell:
    """暂记的卖：下一次更新里出现同名金色时作废（合成和两张普通卡消失可能在相邻两次更新里）。"""

    seq: int
    entity_id: int
    card_id: str
    normal: str


class RecordBuilder:
    def __init__(self, buy_cost: int | BuyCost = 3, normal_id: NormalId | None = None) -> None:
        """buy_cost：买牌价（或按英雄算买牌价的函数，如 RulesConfig.buy_cost_for）；
        normal_id：金色卡对应的普通卡 ID（CardDB.normal_id）；没有卡牌库时去掉 _G。"""
        self._buy_cost: BuyCost = buy_cost if callable(buy_cost) else (lambda _hero: buy_cost)
        self._normal_id: NormalId = normal_id or base_card_id
        self._reset()

    # ---- 对外 ----

    @property
    def ops(self) -> tuple[Op, ...]:
        return tuple(self._ops)

    @property
    def entity_cards(self) -> Mapping[int, str]:
        """本局商店、手牌、场上见过的实体编号 -> 卡牌 ID。"""
        return self._entity_cards

    def on_update(self, state: GameState, events: Sequence[Event]) -> None:
        types = {e.type for e in events}
        if EventType.GAME_STARTED in types:
            self._reset()
        if self._record is not None or state.phase == Phase.WAITING:
            return
        self._active = True
        self._observe_cards(state)
        if state.me is not None and state.me.hero_card_id:
            self._hero = state.me.hero_card_id
        self._observe_choices(state, events)
        if state.phase == Phase.RECRUIT and state.me is not None:
            self._recruit(state, types)
        elif state.phase in (Phase.COMBAT, Phase.GAME_OVER):
            self._close_turn()
            if EventType.COMBAT_STARTED in types:
                self._start_combat(state, events)
            if EventType.COMBAT_ENDED in types:
                self._capture_opponent(state)
        if EventType.GAME_ENDED in types or state.phase == Phase.GAME_OVER:
            self._end_game(state, events)

    def finish(self) -> GameRecord | None:
        """对局结束（GAME_ENDED）后的记录；还没结束或没有对局时 None。"""
        return self._record

    # ---- 内部 ----

    def _reset(self) -> None:
        self._active = False
        self._record: GameRecord | None = None
        self._hero: str | None = None
        self._turns: list[_Turn] = []
        self._current: _Turn | None = None
        self._combats: list[CombatRecord] = []
        self._combat: _Combat | None = None
        self._choices: dict[int, _Choice] = {}
        self._ops: list[Op] = []
        self._entity_cards: dict[int, str] = {}
        self._seen: set[str] = set()
        self._held: list[_HeldSell] = []
        self._recent_goldens: Counter[str] = Counter()  # 上一次更新里新出现的金色（还能吃几张）

    def _see(self, card_id: str) -> None:
        if card_id:
            self._seen.add(card_id)
            self._seen.add(self._normal_id(card_id))

    def _observe_cards(self, state: GameState) -> None:
        for item in state.shop:
            self._see(item.card_id)
            self._entity_cards[item.entity_id] = item.card_id
        me = state.me
        if me is not None:
            for card in (*me.board, *me.hand):
                self._see(card.card_id)
                self._entity_cards[card.entity_id] = card.card_id
        if state.choice is not None:
            for option in state.choice.options:
                self._see(option.card_id)
        for opp in state.opponents.values():
            for m in opp.last_board:
                self._see(m.card_id)

    def _observe_choices(self, state: GameState, events: Sequence[Event]) -> None:
        for event in events:
            if event.type == EventType.CHOICE_OFFERED:
                choice = state.choice
                if choice is None or choice.id != event.data.get("id"):
                    continue
                options = {o.entity_id: (o.card_id, o.cost) for o in choice.options}
                self._choices[choice.id] = _Choice(
                    choice.id, event.turn or state.turn, choice.kind.value, options
                )
            elif event.type == EventType.CHOICE_RESOLVED:
                pending = self._choices.get(int(event.data.get("id") or 0))
                if pending is not None and event.data.get("card"):
                    pending.card_id = str(event.data["card"])
                    picked = event.data.get("picked")
                    pending.entity_id = int(picked) if picked is not None else None

    # ---- 招募 ----

    def _recruit(self, state: GameState, types: set[EventType]) -> None:
        me = state.me
        assert me is not None
        current = self._current
        same_turn = current is not None and current.turn == state.turn and not current.closed
        started = EventType.RECRUIT_STARTED in types
        if not same_turn and (started or state.main_action):
            self._close_turn()
            self._settle_combat(state)
            self._current = _Turn(
                turn=state.turn,
                partial=not started,
                tier_start=me.tavern_tier,
                gold_start=me.gold,
                health_start=effective_health(state),
                board_start=me.board,
                last=state,
                tier_end=me.tavern_tier,
                gold_end=me.gold,
            )
            self._turns.append(self._current)
            return
        if not same_turn:
            return  # 招募开始（MAIN_ACTION）之前：发铸币、摆商店、选饰品和技能姿态
        assert current is not None
        self._diff(current, current.last, state)
        current.last = state
        current.tier_end = me.tavern_tier
        current.gold_end = me.gold

    def _op(self, turn: _Turn, seq: int, kind: OpKind, entity_id: int | None = None) -> None:
        self._ops.append(Op(turn.turn, seq, kind, entity_id))

    def _diff(self, turn: _Turn, prev: GameState, cur: GameState) -> None:
        pm, cm = prev.me, cur.me
        assert pm is not None and cm is not None
        seq = cur.seq
        delta = cm.gold - pm.gold
        if delta > 0:
            turn.gold_gained += delta
        spent = max(-delta, 0)
        prev_shop = {s.entity_id: s for s in prev.shop}
        cur_shop = {s.entity_id: s for s in cur.shop}
        prev_hand = {h.entity_id: h for h in pm.hand}
        cur_hand = {h.entity_id: h for h in cm.hand}
        prev_board = {m.entity_id: m for m in pm.board}
        cur_board = {m.entity_id: m for m in cm.board}
        owned = cur_hand.keys() | cur_board.keys()
        refreshed = bool(cur_shop) and not (prev_shop.keys() & cur_shop.keys())

        # 铸币的去向：先扣掉认得出的买（进了手牌或场上）、升本和英雄技能，剩下的才是刷新的花费，
        # 或者买第三张直接合成的那张
        buy_cost = self._buy_cost(self._hero)
        untraced: list[ShopItem] = []
        for entity_id, item in prev_shop.items():
            if entity_id in cur_shop:
                continue
            if entity_id in owned:
                self._buy(turn, seq, item)
                spent -= buy_cost if item.card_type == MINION else (item.cost or 0)
            elif not refreshed:
                untraced.append(item)
        if cm.tavern_tier > pm.tavern_tier:
            cost = pm.upgrade_cost if pm.upgrade_cost is not None else max(spent, 0)
            turn.upgrade_gold += cost
            spent -= cost
            self._op(turn, seq, "upgrade")
        if pm.hero_power is not None and cm.hero_power is not None:
            if not hero_power_used(pm.hero_power) and hero_power_used(cm.hero_power):
                cost = min(pm.hero_power.cost, max(spent, 0))
                turn.hero_power += 1
                turn.hero_power_gold += cost
                spent -= cost
                self._op(turn, seq, "hero_power")
        if refreshed:
            paid = max(spent, 0)
            turn.rerolls += 1
            turn.reroll_gold += paid
            turn.paid_rerolls += 1 if paid > 0 else 0
            spent -= paid
            self._op(turn, seq, "refresh")
        if cur.shop and all(s.frozen for s in cur.shop) and not _all_frozen(prev):
            turn.froze = True
            self._op(turn, seq, "freeze")
        for item in untraced:
            price = buy_cost if item.card_type == MINION else (item.cost or 1)
            if spent >= price:
                self._buy(turn, seq, item)
                spent -= price

        for entity_id, card in prev_hand.items():
            if card.card_type == MINION and entity_id in cur_board:
                turn.plays.append(card.card_id)

        # 三连：之前不在手牌、场上、商店里的金色随从
        known = prev_hand.keys() | prev_board.keys() | prev_shop.keys()
        goldens = [
            c
            for c in (*cm.hand, *cm.board)
            if c.entity_id not in known
            and getattr(c, "card_type", MINION) == MINION
            and is_golden(c.card_id, c.golden)
        ]
        for card in goldens:
            turn.triples.append(self._normal_id(card.card_id))
        capacity: Counter[str] = Counter()
        for card in goldens:
            capacity[self._normal_id(card.card_id)] += MERGE_COPIES

        # 上次暂记的卖：这次出现了同名金色就是合成
        for held in self._held:
            if capacity[held.normal] > 0:
                capacity[held.normal] -= 1
                continue
            self._commit_sell(turn, held)
        self._held = []
        if delta > 0:
            gone = [m for entity_id, m in prev_board.items() if entity_id not in owned]
            for m in gone:
                normal = self._normal_id(m.card_id)
                if not is_golden(m.card_id, m.golden):
                    if capacity[normal] > 0:
                        capacity[normal] -= 1
                        continue
                    if self._recent_goldens[normal] > 0:
                        self._recent_goldens[normal] -= 1
                        continue
                self._held.append(_HeldSell(seq, m.entity_id, m.card_id, normal))
        self._recent_goldens = capacity

    def _buy(self, turn: _Turn, seq: int, item: ShopItem) -> None:
        if item.card_type == MINION:
            turn.buys.append(item.card_id)
        else:
            turn.spells.append(item.card_id)
        self._op(turn, seq, "buy", item.entity_id)

    def _commit_sell(self, turn: _Turn, held: _HeldSell) -> None:
        turn.sells.append(held.card_id)
        self._op(turn, held.seq, "sell", held.entity_id)

    def _close_turn(self) -> None:
        """招募结束（进战斗、对局结束、下一回合）：暂记的卖都算数。"""
        turn = self._current
        if turn is None or turn.closed:
            return
        for held in self._held:
            self._commit_sell(turn, held)
        self._held = []
        self._recent_goldens = Counter()
        last = turn.last
        if turn.board_end is None:
            turn.board_end = last.me.board if last.me is not None else ()
        turn.closed = True

    # ---- 战斗 ----

    def _start_combat(self, state: GameState, events: Sequence[Event]) -> None:
        event = next(e for e in events if e.type == EventType.COMBAT_STARTED)
        opponent_id = event.data.get("opponent")
        opp = state.opponents.get(int(opponent_id)) if opponent_id is not None else None
        board = state.me.board if state.me is not None else ()
        turn = self._turns[-1] if self._turns else None
        if turn is not None and turn.turn == state.turn:
            turn.board_end = board
        self._combat = _Combat(
            turn=state.turn,
            opponent_id=int(opponent_id) if opponent_id is not None else None,
            opponent_hero=opp.hero_card_id if opp is not None else _str(event.data.get("hero")),
            opponent_place=opp.place if opp is not None else None,
            opponent_tier=opp.tavern_tier if opp is not None else None,
            opponent_health=opp.health + opp.armor if opp is not None else None,
            ghost=opp is not None and not opp.alive,
            my_health=effective_health(state),
            my_minions=len(board),
            my_stats=stats(board),
        )

    def _capture_opponent(self, state: GameState, combat: _Combat | None = None) -> None:
        """对手阵容：第一次攻击时截下来的 last_board（没截到时个数是 0）。"""
        combat = combat or self._combat
        if combat is None or combat.opponent_id is None:
            return
        opp = state.opponents.get(combat.opponent_id)
        if opp is None or opp.last_seen_turn != combat.turn:
            return
        combat.opponent_minions = len(opp.last_board)
        combat.opponent_stats = stats(opp.last_board)

    def _settle_combat(self, state: GameState) -> None:
        combat = self._combat
        if combat is None:
            return
        self._combat = None
        if not combat.opponent_minions:
            self._capture_opponent(state, combat)
        now = effective_health(state)
        lost = max(combat.my_health - now, 0)
        dealt = 0
        opp = state.opponents.get(combat.opponent_id) if combat.opponent_id is not None else None
        if not combat.ghost and opp is not None and combat.opponent_health is not None:
            dealt = max(combat.opponent_health - (opp.health + opp.armor), 0)
        if dealt > 0:
            result = "win"
        elif lost > 0:
            result = "loss"
        elif combat.ghost:
            result = "ghost"
        else:
            result = "tie"
        self._combats.append(
            CombatRecord(
                turn=combat.turn,
                opponent_id=combat.opponent_id,
                opponent_hero=combat.opponent_hero,
                opponent_place=combat.opponent_place,
                opponent_tier=combat.opponent_tier,
                opponent_minions=combat.opponent_minions,
                opponent_stats=combat.opponent_stats,
                my_minions=combat.my_minions,
                my_stats=combat.my_stats,
                lost=lost,
                dealt=dealt,
                ghost=combat.ghost,
                result=result,
                health_after=now,
            )
        )

    # ---- 结束 ----

    def _end_game(self, state: GameState, events: Sequence[Event]) -> None:
        if not self._active:
            return
        self._close_turn()
        self._settle_combat(state)
        ended = next((e for e in events if e.type == EventType.GAME_ENDED), None)
        place = state.final_place
        if place is None and ended is not None and ended.data.get("place") is not None:
            place = int(ended.data["place"])  # type: ignore[arg-type]
        picks: dict[int, list[ChoicePick]] = {}
        for choice in self._choices.values():
            picked = choice.options.get(choice.entity_id) if choice.entity_id is not None else None
            cost = picked[1] if picked is not None else None
            picks.setdefault(choice.turn, []).append(
                ChoicePick(id=choice.id, kind=choice.kind, card_id=choice.card_id, cost=cost)
            )
        self._record = GameRecord(
            hero_card_id=self._hero,
            place=place,
            tribes=tuple(sorted(state.tribes)),
            turns=tuple(_turn_record(t, tuple(picks.get(t.turn, ()))) for t in self._turns),
            combats=tuple(self._combats),
            final_health=effective_health(state),
            uncertain=state.uncertain,
            seen_cards=frozenset(self._seen),
        )


def _all_frozen(state: GameState) -> bool:
    return bool(state.shop) and all(s.frozen for s in state.shop)


def _str(value: object) -> str | None:
    return str(value) if value is not None else None


def _turn_record(turn: _Turn, choices: tuple[ChoicePick, ...]) -> TurnRecord:
    last = turn.last
    me = last.me
    hand_end = sum(1 for h in me.hand if h.card_type == MINION) if me is not None else 0
    return TurnRecord(
        turn=turn.turn,
        partial=turn.partial,
        tier_start=turn.tier_start,
        tier_end=turn.tier_end,
        gold_start=turn.gold_start,
        gold_end=turn.gold_end,
        gold_gained=turn.gold_gained,
        upgrade_gold=turn.upgrade_gold,
        buys=tuple(turn.buys),
        spells=tuple(turn.spells),
        sells=tuple(turn.sells),
        plays=tuple(turn.plays),
        rerolls=turn.rerolls,
        reroll_gold=turn.reroll_gold,
        paid_rerolls=turn.paid_rerolls,
        froze=turn.froze,
        hero_power=turn.hero_power,
        hero_power_gold=turn.hero_power_gold,
        triples=tuple(turn.triples),
        choices=choices,
        board_start=turn.board_start,
        board_end=turn.board_end if turn.board_end is not None else (),
        hand_end=hand_end,
        health_start=turn.health_start,
    )


# ---- 和建议比较（设计文档第 5 节） ----


@dataclass
class AdviceLog:
    """一个回合里显示过的建议：回合开始的规则建议、没过期的快速建议、最后一条深度分析。
    方案连同它基于的快照编号（Advice.state_seq）。"""

    rules_start: tuple[int, Plan] | None = None
    fast: list[tuple[int, Plan]] = field(default_factory=list)
    deep: Advice | None = None


def _key(kind: str, entity_id: int | None) -> tuple[str, int | None]:
    """买、卖按实体算；升本、刷新、冻结、英雄技能整回合各算一次（技能的目标不比较）。"""
    return (kind, entity_id if kind in ("buy", "sell") else None)


def turn_advice(
    turn: int, log: AdviceLog, ops: Sequence[Op], entity_cards: Mapping[int, str]
) -> TurnAdvice:
    """advised：规则、快速、没过期的深度建议里的动作去重后的个数；followed：其中在建议之后、
    本回合结束前做了的。建议买的牌直到回合结束都没买（刷新掉了也算）记进 missed_buys。"""
    plans: list[tuple[int, Plan]] = []
    if log.rules_start is not None:
        plans.append(log.rules_start)
    plans += log.fast
    deep = log.deep
    deep_upgrade = False
    if deep is not None and deep.expired is None:
        plans.append((deep.state_seq, deep.plan))
        deep_upgrade = any(a.type == "upgrade" for a in deep.plan.actions)
    earliest: dict[tuple[str, int | None], int] = {}
    for seq, plan in plans:
        for action in plan.actions:
            key = _key(action.type, action.entity_id)
            earliest[key] = min(seq, earliest.get(key, seq))
    done = [op for op in ops if op.turn == turn]
    followed = 0
    missed: list[str] = []
    for (kind, entity_id), seq in earliest.items():
        if any(_key(op.kind, op.entity_id) == (kind, entity_id) and op.seq > seq for op in done):
            followed += 1
        elif kind == "buy" and entity_id is not None:
            if not any(op.kind == "buy" and op.entity_id == entity_id for op in done):
                missed.append(entity_cards.get(entity_id, f"#{entity_id}"))
    rules = log.rules_start[1] if log.rules_start is not None else None
    rules_actions = tuple(
        (a.type, entity_cards.get(a.entity_id) if a.entity_id is not None else None)
        for a in (rules.actions if rules is not None else ())
        if a.type != "hero_power"
    )
    return TurnAdvice(
        rules_upgrade=rules is not None and any(a.type == "upgrade" for a in rules.actions),
        deep_summary=deep.plan.summary if deep is not None else "",
        deep_direction=deep.plan.direction if deep is not None else None,
        deep_upgrade=deep_upgrade,
        fast_upgrade=any(a.type == "upgrade" for _seq, plan in log.fast for a in plan.actions),
        advised=len(earliest),
        followed=followed,
        missed_buys=tuple(missed),
        rules_actions=rules_actions,
    )


def attach_advice(
    record: GameRecord,
    ops: Sequence[Op],
    entity_cards: Mapping[int, str],
    logs: Mapping[int, AdviceLog],
    *,
    hero_ai_pick: str | None = None,
    ai_choices: Mapping[int, str] | None = None,
) -> GameRecord:
    """把本局显示过的建议并进记录：每回合的 TurnAdvice、选英雄和选项的 AI 推荐。"""
    ai_choices = ai_choices or {}
    turns = []
    for turn in record.turns:
        log = logs.get(turn.turn)
        advice = turn_advice(turn.turn, log, ops, entity_cards) if log is not None else None
        choices = tuple(
            c.model_copy(update={"ai_card_id": ai_choices.get(c.id)}) for c in turn.choices
        )
        turns.append(turn.model_copy(update={"advice": advice, "choices": choices}))
    return record.model_copy(update={"turns": tuple(turns), "hero_ai_pick": hero_ai_pick})
