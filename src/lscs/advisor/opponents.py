"""对手一览（对手一览与卡池估计设计文档第 5 节）：纯函数，不查卡牌库。

对手的阵容只有交手时才看得到（战斗开始后第一次攻击时截下来），所以这里的阵容都是"最近一次见到的"；
血量、名次和酒馆等级是排行榜上的实时数据。威胁分是 0~100 的粗略相对排名：按最近见到的阵容（见得越久
假设它长得越多）、酒馆等级和血量估，不预测胜负。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from lscs.advisor.context import is_golden, main_tribe_of
from lscs.advisor.names import tribe_zh
from lscs.state.models import GameState, Minion, OpponentView

Namer = Callable[[str], str]
KEY_CARDS = 3  # 关键牌最多几张
BOARD_POINTS = 60  # 场面分上限：活着的对手里（按成长估过的）总身材最大的拿满分
TIER_POINTS = 20  # 等级分上限：6 本拿满
HEALTH_POINTS = 20  # 血量分上限：血量 + 护甲 40 拿满
FULL_TIER = 6
FULL_HEALTH = 40
GROWTH_PER_TURN = 0.1  # 上次见到后每过一回合，假设阵容长大 10%
MAX_GROWTH = 2.0  # 最多翻倍
UNSEEN_PER_TIER = 5  # 没见过阵容：按酒馆等级估场面分，每本 5 分
UNSEEN_MAX = 30


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class OpponentSummary(_Frozen):
    player_id: int
    hero_card_id: str | None
    health: int
    armor: int
    place: int | None
    tavern_tier: int | None
    alive: bool
    seen_turn: int | None  # 最近一次见到阵容的酒馆回合；没见过为 None
    minions: int  # 那次见到几个随从
    main_tribe: str | None  # 阵容里数量最多的种族
    tribe_count: int  # 主种族有几个
    total_stats: int  # 攻 + 血 之和
    golden: int  # 金色几张
    # 最多 3 张（金色优先、酒馆等级高、身材大）。存随从而不是卡牌 ID：只有 PREMIUM 的金色卡
    # （夺金健将）ID 和普通卡一样，显示"(金)"要看 Minion.golden
    key_cards: tuple[Minion, ...]
    threat: int  # 0~100；淘汰为 0
    next: bool  # 下一个对手（战斗中：正在打的）
    strongest: bool  # 最强威胁（活着的里威胁分最高的，只有一个）

    @property
    def empty_board(self) -> bool:
        """交过手，但截到的阵容是空的：显示"见到空场"，威胁的场面分按酒馆等级估。"""
        return self.seen_turn is not None and self.minions == 0


class OpponentOverview(_Frozen):
    turn: int
    # 活着的按名次升序（没有名次的排最后，再按 player_id），淘汰的排在后面按名次
    opponents: tuple[OpponentSummary, ...]
    contested: tuple[tuple[str, int], ...]  # (种族, 几个活着的对手主打它)，按数量降序、种族名升序
    unknown: int  # 活着但还没见过阵容的对手数


def round_half_up(value: float) -> int:
    """四舍五入（Python 的 round 是银行家舍入，30.5 会变成 30）。"""
    return math.floor(value + 0.5)


def _key_order(m: Minion) -> tuple[bool, int, int, str]:
    return (not is_golden(m.card_id, m.golden), -(m.tier or 0), -(m.atk + m.health), m.card_id)


def _place_key(place: int | None, player_id: int) -> tuple[bool, int, int]:
    return (place is None, place or 0, player_id)


def _seen_strength(opp: OpponentView, turn: int) -> float | None:
    """按上次见到后过了几回合放大的总身材；没见过阵容、或者截到的阵容是空的（卖光了随从）时
    None，场面分改按酒馆等级估。"""
    if opp.last_seen_turn is None or not opp.last_board:
        return None
    total = sum(m.atk + m.health for m in opp.last_board)
    growth = min(MAX_GROWTH, 1 + GROWTH_PER_TURN * max(turn - opp.last_seen_turn, 0))
    return total * growth


def _threat(opp: OpponentView, strength: float | None, strongest_seen: float) -> int:
    if not opp.alive:
        return 0
    tier = opp.tavern_tier or 0
    if strength is None:
        board = min(UNSEEN_MAX, UNSEEN_PER_TIER * tier)
    elif strongest_seen > 0:
        board = BOARD_POINTS * strength / strongest_seen
    else:
        board = 0.0
    tier_points = min(TIER_POINTS, TIER_POINTS * tier / FULL_TIER)
    health_points = min(HEALTH_POINTS, HEALTH_POINTS * (opp.health + opp.armor) / FULL_HEALTH)
    return max(0, min(100, round_half_up(board + tier_points + health_points)))


def summarize_opponents(state: GameState) -> OpponentOverview:
    turn = state.turn
    opps = list(state.opponents.values())
    strengths = {o.player_id: _seen_strength(o, turn) for o in opps}
    strongest_seen = max(
        (s for o in opps if o.alive and (s := strengths[o.player_id]) is not None), default=0.0
    )
    threats = {o.player_id: _threat(o, strengths[o.player_id], strongest_seen) for o in opps}
    alive = [o for o in opps if o.alive]
    best = min(
        alive,
        key=lambda o: (-threats[o.player_id], *_place_key(o.place, o.player_id)),
        default=None,
    )
    summaries = []
    for opp in opps:
        board = opp.last_board
        tribe, count = main_tribe_of(board, state.tribes)
        summaries.append(
            OpponentSummary(
                player_id=opp.player_id,
                hero_card_id=opp.hero_card_id,
                health=opp.health,
                armor=opp.armor,
                place=opp.place,
                tavern_tier=opp.tavern_tier,
                alive=opp.alive,
                seen_turn=opp.last_seen_turn,
                minions=len(board),
                main_tribe=tribe,
                tribe_count=count,
                total_stats=sum(m.atk + m.health for m in board),
                golden=sum(1 for m in board if is_golden(m.card_id, m.golden)),
                key_cards=tuple(sorted(board, key=_key_order)[:KEY_CARDS]),
                threat=threats[opp.player_id],
                next=opp.player_id == state.next_opponent_id,
                strongest=best is not None and opp.player_id == best.player_id,
            )
        )
    summaries.sort(key=lambda s: (not s.alive, *_place_key(s.place, s.player_id)))
    living = [s for s in summaries if s.alive]
    tribes = Counter(s.main_tribe for s in living if s.main_tribe is not None)
    return OpponentOverview(
        turn=turn,
        opponents=tuple(summaries),
        contested=tuple(sorted(tribes.items(), key=lambda item: (-item[1], item[0]))),
        unknown=sum(1 for s in living if s.seen_turn is None),
    )


def contested_parts(overview: OpponentOverview) -> list[str]:
    """主打种族的写法，如"畸变 4 家"（终端、面板和 AI 共用；"未知"由调用方按自己的写法加）。"""
    return [f"{tribe_zh(tribe)} {count} 家" for tribe, count in overview.contested]


def eliminated_names(overview: OpponentOverview, name: Namer) -> list[str]:
    """已淘汰、又不是下一个对手（残影）的对手的英雄名，按名次（终端和局面消息合成一行）。"""
    return [
        name(s.hero_card_id) if s.hero_card_id else "未知"
        for s in overview.opponents
        if not s.alive and not s.next
    ]


def stats_text(s: OpponentSummary) -> str:
    """opponent_text(stats=False) 省掉的那一段：活着的"畸变×5 身材 95"，残影"4 个 · 身材 29"；
    没见过、空场、淘汰（不是残影）时为空。"""
    if s.seen_turn is None or s.empty_board or (not s.alive and not s.next):
        return ""
    if not s.alive:
        return f"{s.minions} 个 · 身材 {s.total_stats}"
    stats = f"身材 {s.total_stats}"
    if s.main_tribe is not None:
        stats = f"{tribe_zh(s.main_tribe)}×{s.tribe_count} {stats}"
    return stats


def opponent_text(
    s: OpponentSummary,
    name: Namer,
    turn: int,
    *,
    place: bool,
    seen: bool,
    threat: bool,
    stats: bool = True,
) -> str:
    """一个对手的一行（面板详细模式战斗中的"对手"块和对手阵容卡图窗口的标题行共用）：
    名字 血量+护甲 · N本 · 主种族×数 身材 S（· 第 T 回合见）（· 威胁 N）；淘汰的只写"已淘汰"，
    残影写它最后的阵容有几个、多大。place：前面加名次。stats=False：不写阵容的大小（活着的
    "主种族×数 身材 S"、残影的"N 个 · 身材 S"，见 stats_text），卡图窗口放不下时先省它。"""
    hero = name(s.hero_card_id) if s.hero_card_id else "未知"
    head = f"第{s.place if s.place is not None else '?'}名 {hero}" if place else hero
    when = "本回合见" if s.seen_turn == turn else f"第 {s.seen_turn} 回合见"
    size = stats_text(s) if stats else ""
    if not s.alive:
        if not s.next:
            return f"{head} 已淘汰"
        if s.seen_turn is None:
            return f"{head} 已淘汰（残影）· 还没见过阵容"
        if s.empty_board:
            return f"{head} 已淘汰（残影）· {when}到空场"
        return f"{head} 已淘汰（残影）· {when} {size}" if size else f"{head} 已淘汰（残影）· {when}"
    tier = "?" if s.tavern_tier is None else s.tavern_tier
    parts = [f"{head} {s.health}+{s.armor}", f"{tier}本"]
    if s.seen_turn is None:
        parts.append("还没见过阵容")
    elif s.empty_board:
        parts.append(f"{when}到空场" if seen else "见到空场")
    else:
        if size:
            parts.append(size)
        if seen:
            parts.append(when)
    if threat:
        parts.append(f"威胁 {s.threat}")
    return " · ".join(parts)
