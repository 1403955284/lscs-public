"""提醒（对局复盘设计文档第 4 节）：从对局记录里按确定性的规则找出值得说的回合，
连同一句事实和一句"下次怎么做"（模板生成）；再挑出 2~3 个关键时刻。

严重度：0 只写进文件；1 一般；2 要紧；3 很要紧。同一种提醒在相邻回合触发时合并成一条；
连续掉血的起点落在"升本不补场面"的回合或下一回合时，并进那一条。中途启动的回合（partial）
不触发任何提醒。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from lscs.advisor.context import main_tribe_of
from lscs.advisor.names import tribe_zh
from lscs.advisor.rules_config import RulesConfig
from lscs.review.record import CombatRecord, GameRecord, TurnRecord

Namer = Callable[[str], str]
FlagKind = Literal[
    "tier_behind",
    "upgrade_without_board",
    "reroll_heavy",
    "gold_unspent",
    "hp_streak",
    "no_direction",
    "advice_upgrade_ignored",
    "advice_buys_ignored",
    "choice_disagree",
    "good_turn",
]
KIND_NAMES: dict[str, str] = {
    "tier_behind": "升本偏晚",
    "upgrade_without_board": "升本不补场面",
    "reroll_heavy": "刷新太多",
    "gold_unspent": "铸币没花完",
    "hp_streak": "连续掉血",
    "no_direction": "方向不定",
    "advice_upgrade_ignored": "没按建议升本",
    "advice_buys_ignored": "建议买的没买",
    "choice_disagree": "选项和 AI 不同",
    "good_turn": "做得好",
}
SEVERITY_NAMES = {0: "提示", 1: "一般", 2: "要紧", 3: "很要紧"}

CHECKPOINTS = (4, 6, 8, 10)  # 看升本节奏的回合
# 回合末场上随从数的基准；第 9 回合起 7 个
BOARD_FLOOR = {3: 2, 4: 3, 5: 4, 6: 5, 7: 6, 8: 7}
FULL_BOARD = 7
REROLLS = 4  # 一回合刷新这么多次、只买了 0~1 张：刷新太多
MANY_REROLLS = 6
GOLD_TOTAL = 6  # 没花的铸币合计这么多：要紧
STREAK = 3  # 连续这么多场掉血
STREAK_PAIR_LOST = 20  # 或者连续两场合计掉这么多
LOW_HEALTH = 10  # 连续掉血后只剩这么多：很要紧
BIG_HIT = 10  # 一场掉（或打出）这么多算大
DIRECTION_TURN = 7  # 从这回合起看有没有主方向
DIRECTION_BOARD = 5  # 场上至少这么多个随从时才看
DIRECTION_MIN = 3  # 主种族少于这么多个：方向不定
MISSED_BUYS = 3  # 建议买而没买的张数
CHOICE_DISAGREE = 3  # 选项和 AI 不同的次数

FIXES: dict[str, str] = {
    "tier_behind": "血量健康时按节奏升本",  # 提醒本身写完整的节奏表（按 rules.toml 的 curve）
    "upgrade_without_board": (
        "升本的回合剩下的铸币也要买一张随从；场上比基准少 2 个以上时先补场面、下回合再升本"
    ),
    "reroll_heavy": "刷新前先想好要找什么：有对子找第三张、有方向找核心卡；没目标时把钱买成随从",
    "gold_unspent": "铸币不会留到下回合：剩 3 金买随从，剩 1~2 金刷新",
    "hp_streak": "连掉两场就先补战力：升本缓一缓，买身材最大的随从、找嘲讽和圣盾",
    "no_direction": "中期挑一个种族做主方向，买牌优先选它",
    "advice_upgrade_ignored": "建议升本的回合优先升本，剩下的钱再买牌",
    "advice_buys_ignored": "建议买的牌一般是对子或身材最好的那张，不买要有自己的理由",
    "choice_disagree": "选项不确定时跟 AI；想自己选时想清楚它和场上怎么配合",
    "good_turn": "",
}
# 升本把铸币花光了（剩的不够买一张）：再叫他"剩下的铸币买随从"是自相矛盾，改成先补场面
BOARD_FIRST = "场上比基准少时先补场面、下回合再升本"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Flag(_Frozen):
    kind: FlagKind
    turns: tuple[int, ...]  # 涉及的回合（连续的同类提醒合并成一条）
    severity: int  # 0 只写进文件；1 一般；2 要紧；3 很要紧
    facts: str  # 一句事实，模板生成（不带"第 N 回合"，标题里有）
    fix: str  # 一句"下次怎么做"；做得好的没有
    streak_turns: tuple[int, ...] = ()  # 并进来的连续掉血的回合
    streak_lost: int = 0  # 并进来的连续掉血一共掉了多少
    weight: int = 0  # 做得好的回合有几件好事（三连、打出 10 血、升本还买了随从），挑领头的用

    @property
    def name(self) -> str:
        return KIND_NAMES[self.kind]

    @property
    def title(self) -> str:
        """ "第 7、8 回合 升本不补场面"。"""
        return f"{turns_text(self.turns)} {self.name}"

    @property
    def covered(self) -> frozenset[int]:
        return frozenset(self.turns) | frozenset(self.streak_turns)


def turns_text(turns: Sequence[int]) -> str:
    return "第 " + "、".join(str(t) for t in turns) + " 回合"


def _join(values: Iterable[object], unit: str = "") -> str:
    return "、".join(f"{v}{unit}" for v in values)


def _groups(turns: Sequence[int], gap: int = 1) -> list[list[int]]:
    """把回合按相邻（相差不超过 gap）分组。"""
    groups: list[list[int]] = []
    for turn in sorted(turns):
        if groups and turn - groups[-1][-1] <= gap:
            groups[-1].append(turn)
        else:
            groups.append([turn])
    return groups


def board_floor(turn: int) -> int | None:
    """第 turn 回合末场上该有几个随从；第 3 回合之前不看。"""
    if turn < min(BOARD_FLOOR):
        return None
    return BOARD_FLOOR.get(turn, FULL_BOARD)


class _Finder:
    def __init__(self, record: GameRecord, rules: RulesConfig, name: Namer) -> None:
        self.record = record
        self.rules = rules
        self.name = name
        self.turns = [t for t in record.turns if not t.partial]
        self.by_turn = {t.turn: t for t in self.turns}

    def healthy(self, turn: TurnRecord) -> bool:
        return turn.health_start > self.rules.safe_below

    # ---- 各种提醒 ----

    def tier_behind(self) -> list[Flag]:
        behind: dict[int, tuple[int, int]] = {}  # 回合 -> (实际等级, 该有的等级)
        for checkpoint in CHECKPOINTS:
            turn = self.by_turn.get(checkpoint)
            if turn is None or not self.healthy(turn):
                continue
            target = self.rules.target_tier(checkpoint, 0)
            if turn.tier_end < target:
                behind[checkpoint] = (turn.tier_end, target)
        flags = []
        for group in _groups(list(behind), gap=2):
            tiers = [behind[t][0] for t in group]
            targets = [behind[t][1] for t in group]
            gap = max(target - tier for tier, target in zip(tiers, targets, strict=True))
            severity = 2 if gap >= 2 or len(group) >= 2 else 1
            if len(group) == 1:
                facts = f"结束时还是 {tiers[0]} 本，新手节奏这时该 {targets[0]} 本"
            else:
                facts = (
                    f"结束时分别是 {_join(tiers, ' 本')}，新手节奏这时该 {_join(targets, ' 本')}"
                )
            flags.append(
                Flag(
                    kind="tier_behind",
                    turns=tuple(group),
                    severity=severity,
                    facts=facts,
                    fix=self.curve_fix(),
                )
            )
        return flags

    def curve_fix(self) -> str:
        curve = sorted(self.rules.curve.items())
        turns = "、".join(str(t) for t, _ in curve)
        tiers = "、".join(str(tier) for _, tier in curve)
        return f"血量健康时按节奏升本：第 {turns} 回合升到 {tiers} 本"

    def upgrade_without_board(self) -> list[Flag]:
        hits = []
        for turn in self.turns:
            floor = board_floor(turn.turn)
            if turn.upgraded and not turn.buys and floor is not None:
                if len(turn.board_end) < floor:
                    hits.append(turn.turn)
        flags = []
        price = self.rules.buy_cost_for(self.record.hero_card_id)
        for group in _groups(hits):
            turns = [self.by_turn[t] for t in group]
            boards = [len(t.board_end) for t in turns]
            floor = board_floor(group[-1])
            short = max((board_floor(t.turn) or 0) - len(t.board_end) for t in turns)
            # 只比基准少 1 个、血量又健康：一般；少得多：要紧；之后马上大掉血：很要紧
            severity = 1 if short <= 1 and all(self.healthy(t) for t in turns) else 2
            for t in (*group, group[-1] + 1):
                combat = self.record.combat(t)
                if combat is not None and combat.lost >= BIG_HIT:
                    severity = 3
            if len(turns) == 1:
                t = turns[0]
                spent = f"花光 {t.gold_start} 金" if t.gold_end == 0 else f"剩 {t.gold_end} 金"
                facts = f"升 {t.tier_end} 本{spent}，没买随从，场上只有 {boards[0]} 个"
            else:
                tiers = _join((t.tier_end for t in turns), " 本")
                if len(set(boards)) == 1:
                    have = f"场上一直只有 {boards[0]} 个"
                else:
                    have = f"场上只有 {_join(boards)} 个"
                facts = f"连续升本（{tiers}）但没买随从，{have}"
            facts += f"（这时该有 {floor} 个）"
            # 升本把铸币花光了，剩的不够买一张：下次该先补场面；剩的够买：下次升本后接着买
            spent_out = all(t.gold_start - t.upgrade_gold < price for t in turns)
            fix = BOARD_FIRST if spent_out else FIXES["upgrade_without_board"]
            flags.append(
                Flag(
                    kind="upgrade_without_board",
                    turns=tuple(group),
                    severity=severity,
                    facts=facts,
                    fix=fix,
                )
            )
        return flags

    def reroll_heavy(self) -> list[Flag]:
        # 只算花了铸币的刷新（英雄技能、法术给的免费刷新不算）；刷新找到了三连的回合，
        # 刷到 6 次以上才算
        hits = [
            t.turn
            for t in self.turns
            if t.paid_rerolls >= REROLLS
            and len(t.buys) <= 1
            and (not t.triples or t.paid_rerolls >= MANY_REROLLS)
        ]
        flags = []
        for group in _groups(hits):
            turns = [self.by_turn[t] for t in group]
            rerolls = [t.paid_rerolls for t in turns]
            buys = [len(t.buys) for t in turns]
            severity = 2 if max(rerolls) >= MANY_REROLLS else 1
            if len(turns) == 1:
                facts = f"刷了 {rerolls[0]} 次只买了 {buys[0]} 张"
            else:
                facts = f"分别刷了 {_join(rerolls, ' 次')}，只买了 {_join(buys, ' 张')}"
            flags.append(
                Flag(
                    kind="reroll_heavy",
                    turns=tuple(group),
                    severity=severity,
                    facts=facts,
                    fix=FIXES["reroll_heavy"],
                )
            )
        return flags

    def gold_unspent(self) -> list[Flag]:
        price = self.rules.buy_cost_for(self.record.hero_card_id)
        left = [t for t in self.turns if t.gold_end >= price]
        if not left:
            return []
        total = sum(t.gold_end for t in left)
        if len(left) == 1:
            facts = f"剩了 {left[0].gold_end} 金没用"
        else:
            facts = f"分别剩了 {_join((t.gold_end for t in left), ' 金')}没用（共 {total} 金）"
        return [
            Flag(
                kind="gold_unspent",
                turns=tuple(t.turn for t in left),
                severity=2 if total >= GOLD_TOTAL else 1,
                facts=facts,
                fix=FIXES["gold_unspent"],
            )
        ]

    def hp_streak(self) -> list[Flag]:
        combats = sorted(self.record.combats, key=lambda c: c.turn)
        runs: list[list[CombatRecord]] = []
        for combat in combats:
            if combat.lost <= 0 or combat.turn not in self.by_turn:
                continue
            if runs and combat.turn == runs[-1][-1].turn + 1:
                runs[-1].append(combat)
            else:
                runs.append([combat])
        flags = []
        for run in runs:
            lost = sum(c.lost for c in run)
            if len(run) < STREAK and not (len(run) == 2 and lost >= STREAK_PAIR_LOST):
                continue
            if len(run) < 2:
                continue
            after = run[-1].health_after
            severity = 3 if after <= LOW_HEALTH else 2
            flags.append(
                Flag(
                    kind="hp_streak",
                    turns=tuple(c.turn for c in run),
                    severity=severity,
                    facts=streak_facts(run),
                    fix=FIXES["hp_streak"],
                )
            )
        return flags

    def no_direction(self) -> list[Flag]:
        hits: dict[int, tuple[int, str | None, int]] = {}
        lobby = frozenset(self.record.tribes)
        for turn in self.turns:
            board = turn.board_end
            if turn.turn < DIRECTION_TURN or len(board) < DIRECTION_BOARD:
                continue
            tribe, count = main_tribe_of(board, lobby)
            if tribe is not None:  # 全种族的随从也算进主方向
                count += sum(1 for m in board if "ALL" in m.races and tribe not in m.races)
            if count < DIRECTION_MIN:
                hits[turn.turn] = (len(board), tribe, count)
        flags = []
        for group in _groups(list(hits)):
            size, tribe, count = hits[group[-1]]
            if tribe is None:
                facts = f"场上 {size} 个随从都没有种族"
            else:
                facts = f"场上 {size} 个随从，最多的{tribe_zh(tribe)}也只有 {count} 个"
            flags.append(
                Flag(
                    kind="no_direction",
                    turns=tuple(group),
                    severity=1,
                    facts=facts,
                    fix=FIXES["no_direction"],
                )
            )
        return flags

    def advice_upgrade_ignored(self, behind: Sequence[Flag]) -> list[Flag]:
        hits = [
            t.turn
            for t in self.turns
            if t.advice is not None
            and t.advice.upgrade_advised
            and not t.upgraded
            and self.healthy(t)
        ]
        late = {turn for flag in behind for turn in flag.turns}
        flags = []
        for group in _groups(hits):
            turns = [self.by_turn[t] for t in group]
            # 之后的第一个检查点落后了：要紧
            nxt = next((c for c in CHECKPOINTS if c >= group[0]), None)
            severity = 2 if nxt in late else 1
            facts = f"建议升 {_join((t.tier_end + 1 for t in turns), ' 本')}，你没升"
            flags.append(
                Flag(
                    kind="advice_upgrade_ignored",
                    turns=tuple(group),
                    severity=severity,
                    facts=facts,
                    fix=FIXES["advice_upgrade_ignored"],
                )
            )
        return flags

    def advice_buys_ignored(self) -> list[Flag]:
        missed = [
            (t.turn, t.advice.missed_buys) for t in self.turns if t.advice and t.advice.missed_buys
        ]
        count = sum(len(cards) for _, cards in missed)
        if count < MISSED_BUYS:
            return []
        names: list[str] = []
        for _, cards in missed:
            for card in cards:
                label = self.name(card)
                if label not in names:
                    names.append(label)
        shown = "、".join(names[:3]) + ("等" if len(names) > 3 else "")
        facts = f"建议买的 {count} 张牌没买（{shown}）"
        return [
            Flag(
                kind="advice_buys_ignored",
                turns=tuple(t for t, _ in missed),
                severity=1,
                facts=facts,
                fix=FIXES["advice_buys_ignored"],
            )
        ]

    def choice_disagree(self) -> list[Flag]:
        picks = [
            (t.turn, c)
            for t in self.turns
            for c in t.choices
            if c.ai_card_id and c.card_id and c.ai_card_id != c.card_id
        ]
        if not picks:
            return []
        examples = "；".join(
            f"AI 推荐 {self.name(c.ai_card_id or '')}，你选了 {self.name(c.card_id or '')}"
            for _, c in picks[:2]
        )
        turns = tuple(sorted({t for t, _ in picks}))
        return [
            Flag(
                kind="choice_disagree",
                turns=turns,
                severity=1 if len(picks) >= CHOICE_DISAGREE else 0,
                facts=f"{len(picks)} 次选项没按 AI 选（{examples}）",
                fix=FIXES["choice_disagree"],
            )
        ]

    def good_turns(self) -> list[Flag]:
        flags = []
        for turn in self.turns:
            parts = []
            if turn.triples:
                parts.append("凑出" + "、".join(f"金色{self.name(c)}" for c in turn.triples))
            combat = self.record.combat(turn.turn)
            if combat is not None and combat.dealt >= BIG_HIT:
                parts.append(f"打出 {combat.dealt} 血")
            if turn.upgraded and turn.buys:
                parts.append(f"升 {turn.tier_end} 本的同时还买了 {len(turn.buys)} 张随从")
            if parts:
                flags.append(
                    Flag(
                        kind="good_turn",
                        turns=(turn.turn,),
                        severity=0,
                        facts="，".join(parts),
                        fix="",
                        weight=len(parts),
                    )
                )
        return flags


def streak_facts(run: Sequence[CombatRecord]) -> str:
    lost = sum(c.lost for c in run)
    after = run[-1].health_after
    if after <= 0:
        return f"连掉 {lost} 血，被淘汰"
    return f"连掉 {lost} 血，只剩 {after} 血"


def _merge_streaks(flags: list[Flag], record: GameRecord) -> list[Flag]:
    """连续掉血的起点落在某条"升本不补场面"的回合或下一回合时，并进那一条。"""
    out = list(flags)
    for streak in [f for f in flags if f.kind == "hp_streak"]:
        index = next(
            (
                i
                for i, f in enumerate(out)
                if f.kind == "upgrade_without_board"
                and streak.turns[0] in (*f.turns, f.turns[-1] + 1)
            ),
            None,
        )
        if index is None:
            continue
        host = out[index]
        run = [c for c in record.combats if c.turn in streak.turns]
        lost = sum(c.lost for c in run)
        before = run[0].health_after + run[0].lost
        tail = (
            f"，接着{turns_text(streak.turns)}连掉 {lost} 血（{before} → {run[-1].health_after}）"
        )
        out[index] = host.model_copy(
            update={
                "facts": host.facts + tail,
                "severity": max(host.severity, streak.severity),
                "streak_turns": streak.turns,
                "streak_lost": lost,
            }
        )
        out.remove(streak)
    return out


def find_flags(record: GameRecord, rules: RulesConfig, name: Namer) -> tuple[Flag, ...]:
    """本局的所有提醒，按第一个回合、再按种类排好。"""
    finder = _Finder(record, rules, name)
    behind = finder.tier_behind()
    flags = [
        *behind,
        *finder.upgrade_without_board(),
        *finder.reroll_heavy(),
        *finder.gold_unspent(),
        *finder.hp_streak(),
        *finder.no_direction(),
        *finder.advice_upgrade_ignored(behind),
        *finder.advice_buys_ignored(),
        *finder.choice_disagree(),
        *finder.good_turns(),
    ]
    flags = _merge_streaks(flags, record)
    order = list(KIND_NAMES)
    return tuple(sorted(flags, key=lambda f: (f.turns[0], order.index(f.kind))))


SHORT_GAME = 5  # 对局不到这么多回合时最多 1 个关键时刻
MAX_MOMENTS = 3


# 升本偏晚和没按建议升本说的是同一件事：相邻回合都触发时只挑严重度高的那条
SAME_STORY = frozenset({"tier_behind", "advice_upgrade_ignored"})


def _same_story(flag: Flag, chosen: Sequence[Flag]) -> bool:
    if flag.kind not in SAME_STORY:
        return False
    return any(
        other.kind in SAME_STORY
        and other.kind != flag.kind
        and min(abs(a - b) for a in flag.turns for b in other.turns) <= 1
        for other in chosen
    )


def pick_moments(flags: Sequence[Flag], turns: int, winner: bool = False) -> tuple[Flag, ...]:
    """关键时刻：严重度 ≥ 1 的按（严重度降序、第一个回合升序）取前 3 条，回合不重复；
    不足 3 条时按回合补"做得好"（打得好的局也有话说）。对局不到 5 个回合最多 1 条。
    winner：第 1 名、一滴血没掉的局，先写最好的那个"做得好"的回合。"""
    limit = 1 if turns < SHORT_GAME else MAX_MOMENTS
    chosen: list[Flag] = []
    used: set[int] = set()
    bad = sorted((f for f in flags if f.severity >= 1), key=lambda f: (-f.severity, f.turns[0]))
    good = sorted((f for f in flags if f.kind == "good_turn"), key=lambda f: f.turns[0])
    lead = [max(good, key=lambda f: (f.weight, -f.turns[0]))] if winner and good else []
    for flag in [*lead, *bad, *good]:
        if len(chosen) >= limit:
            break
        if flag.covered & used or _same_story(flag, chosen):
            continue
        chosen.append(flag)
        used |= flag.covered
    return tuple(chosen)


GENERIC_FOCUS = "保持这局的打法：按节奏升本，每回合把铸币花完"


def first_clause(text: str) -> str:
    return text.split("；", 1)[0]


def rules_focus(moments: Sequence[Flag]) -> str:
    """规则版的"下局重点"：第一条有"下次怎么做"的关键时刻的第一句。"""
    fix = next((m.fix for m in moments if m.fix), "")
    return first_clause(fix) if fix else GENERIC_FOCUS
