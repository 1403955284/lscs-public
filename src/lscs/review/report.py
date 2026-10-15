"""复盘的输出（对局复盘设计文档第 7 节）：终端的一块、Markdown 文件和文件名、
history.jsonl 和"最近 N 局"的习惯。

文件里只有英雄名、卡名和数字：复盘只用 GameState，里面没有任何玩家名字。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from lscs.advisor.names import CHOICE_TITLES, tribe_zh
from lscs.advisor.rules_config import RulesConfig
from lscs.review.flags import (
    FIXES,
    KIND_NAMES,
    SEVERITY_NAMES,
    Flag,
    find_flags,
    first_clause,
    pick_moments,
    rules_focus,
)
from lscs.review.record import ChoicePick, CombatRecord, GameRecord, TurnRecord, stats
from lscs.state.models import ChoiceKind

if TYPE_CHECKING:
    from lscs.review.ai import AIReview

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
HISTORY_FILE = "history.jsonl"
VERSION_MARK = "<!-- lscs review v1 -->"
UNCERTAIN = "⚠ 日志解析出错过，复盘可能不准"
NO_FLAGS = "这局没有触发提醒"
CIRCLED = "①②③④⑤"
INDENT = "     "  # 复盘块里每一行的缩进（和"📝 "后面的文字对齐）
SUB_INDENT = "        "  # "下次："这类第二行
LIST_NAMES = 4  # 一回合买、卖、打出的牌超过这么多张时只写张数
HABIT_SKIP = frozenset({"good_turn", "choice_disagree"})  # 不算习惯的提醒
MIN_HABIT_GAMES = 3  # 历史满这么多局才看习惯
MIN_HABIT_COUNT = 3  # 一种提醒至少出现在这么多局里
HABIT_SHARE = 3  # 而且至少占最近几局的 1/3
MAX_HABITS = 3
CHECKPOINTS = (4, 6, 8, 10)  # history 里记酒馆等级的回合
BOARD_TURNS = (3, 5, 7, 9)  # history 里记场上随从数的回合
_UNSAFE = re.compile(r"[^\w一-鿿·-]")


# ---- 历史和习惯 ----


@dataclass(frozen=True)
class Habits:
    """最近几局（至少 3 局）的平均名次和常见的提醒。"""

    games: int
    avg_place: float | None
    kinds: tuple[tuple[str, int], ...]  # (提醒种类, 出现在几局里)，局数降序

    def parts(self) -> list[str]:
        parts = [f"平均第 {self.avg_place:.1f} 名"] if self.avg_place is not None else []
        return parts + [f"{KIND_NAMES.get(kind, kind)} {count} 局" for kind, count in self.kinds]

    def line(self) -> str:
        """终端："最近 5 局：平均第 3.4 名 · 升本不补场面 3 局"。"""
        return f"最近 {self.games} 局：" + (" · ".join(self.parts()) or "还看不出习惯")

    def sentence(self) -> str:
        """文件："平均第 3.4 名；升本不补场面 3 局、刷新太多 2 局。"。"""
        head = f"平均第 {self.avg_place:.1f} 名" if self.avg_place is not None else ""
        kinds = "、".join(f"{KIND_NAMES.get(k, k)} {c} 局" for k, c in self.kinds)
        text = "；".join(part for part in (head, kinds) if part)
        return (text or "还看不出习惯") + "。"


def flag_kinds(flags: Sequence[Flag]) -> list[str]:
    """写进 history 的提醒种类：严重度 ≥ 1 的；并进来的连续掉血也算一条。"""
    kinds = []
    for flag in flags:
        if flag.severity < 1:
            continue
        kinds.append(flag.kind)
        if flag.streak_turns:
            kinds.append("hp_streak")
    return kinds


def read_history(path: Path) -> list[dict]:
    """读不到时为空；坏掉的行跳过，只记日志。"""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError):
        log.warning("读取复盘历史 %s 失败", path, exc_info=True)
        return []
    entries = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            log.warning("复盘历史 %s 第 %d 行坏了，跳过", path, number)
            continue
        if isinstance(entry, dict):
            entries.append(entry)
        else:
            log.warning("复盘历史 %s 第 %d 行不是对象，跳过", path, number)
    return entries


def append_history(path: Path, entry: dict) -> None:
    """失败时抛出 OSError（调用方只记日志）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def habits(entries: Sequence[dict], n: int) -> Habits | None:
    """最近 n 局的习惯；不足 3 局时为 None。一种提醒出现在至少 3 局里、而且至少占这几局的
    1/3，才算习惯；按局数降序最多 3 条。"""
    recent = list(entries)[-n:] if n > 0 else []
    if len(recent) < MIN_HABIT_GAMES:
        return None
    places = [e["place"] for e in recent if isinstance(e.get("place"), int)]
    avg = round(sum(places) / len(places), 1) if places else None
    counts: Counter[str] = Counter()
    for entry in recent:
        flags = entry.get("flags")
        if isinstance(flags, list):
            counts.update({k for k in flags if k in KIND_NAMES and k not in HABIT_SKIP})
    order = list(KIND_NAMES)
    common = [
        (kind, count)
        for kind, count in counts.items()
        if count >= MIN_HABIT_COUNT and count * HABIT_SHARE >= len(recent)
    ]
    common.sort(key=lambda kc: (-kc[1], order.index(kc[0])))
    return Habits(games=len(recent), avg_place=avg, kinds=tuple(common[:MAX_HABITS]))


# 认同一局用的字段：watch 重新启动时会从头读同一份 Power.log，最后那局已经复盘过了
FINGERPRINT = (
    "hero",
    "place",
    "turns",
    "upgrade_turns",
    "rerolls",
    "hp_lost",
    "loss_combats",
    "triples",
)


def already_reviewed(entry: dict, history: Sequence[dict]) -> dict | None:
    """history 里同一天、指纹相同的那一局；没有时 None。"""
    day = str(entry.get("time", ""))[:10]
    key = [entry.get(k) for k in FINGERPRINT]
    for old in reversed(history):
        if str(old.get("time", ""))[:10] == day and [old.get(k) for k in FINGERPRINT] == key:
            return old
    return None


def history_line(entry: dict) -> str:
    """lscs review --habits 里一局一行："2026-10-11 23:20 凯瑞尔·罗姆 第 3 名 · 升本不补场面"。"""
    when = entry.get("time")
    when = when[:16].replace("T", " ") if isinstance(when, str) else "时间未知"
    hero = entry.get("hero_name") or entry.get("hero") or "未知英雄"
    place = entry.get("place")
    head = f"{when} {hero} {place_text(place if isinstance(place, int) else None)}"
    flags = entry.get("flags")
    kinds: list[str] = []
    for kind in flags if isinstance(flags, list) else ():
        label = KIND_NAMES.get(kind) if isinstance(kind, str) else None
        if label and kind not in HABIT_SKIP and label not in kinds:
            kinds.append(label)
    return head + (" · " + "、".join(kinds) if kinds else " · 没有提醒")


def _has_kind(flag: Flag, kind: str) -> bool:
    return flag.kind == kind or (kind == "hp_streak" and bool(flag.streak_turns))


def final_focus(focus: str, flags: Sequence[Flag], habit: Habits | None) -> str:
    """ "下局重点"：最常见的习惯这局也犯了时，换成那条的"下次怎么做"并注明最近几局里几局。"""
    if habit is None or not habit.kinds:
        return focus
    kind, count = habit.kinds[0]
    flag = next((f for f in flags if f.severity >= 1 and _has_kind(f, kind)), None)
    if flag is None:
        return focus
    # 这局那条提醒自己的"下次怎么做"（按情况选的）；并进来的连续掉血用它的模板
    fix = flag.fix if flag.kind == kind else FIXES.get(kind, "")
    return f"{first_clause(fix)}（最近 {habit.games} 局里 {count} 局）"


# ---- 复盘 ----

# off：不调 AI；pending：AI 复盘生成中；done：AI 复盘到了（ai 不为 None）；failed：失败，用规则版
AIState = Literal["off", "pending", "done", "failed"]


@dataclass(frozen=True)
class GameReview:
    record: GameRecord
    flags: tuple[Flag, ...]
    moments: tuple[Flag, ...]
    focus: str  # 下局重点
    hero_name: str
    when: datetime  # 对局结束的时间（文件名、标题）
    habits: Habits | None = None
    path: Path | None = None  # 保存的文件；没保存时 None
    shown_path: str | None = None  # 显示用的路径（项目目录下的写相对路径）
    save_error: str | None = None  # 没能保存的原因
    ai: AIReview | None = None
    ai_state: AIState = "off"
    ai_error: str | None = None  # AI 复盘失败的原因
    ai_elapsed: float | None = None  # AI 复盘用了几秒


def turn_count(record: GameRecord) -> int:
    return record.last_turn


def turns_label(record: GameRecord, unit: str = " 回合") -> str:
    """ "13 回合"；中途开始记录时"13 回合（从第 7 回合起记录）"。"""
    text = f"{record.last_turn}{unit}"
    if record.recorded_from is not None:
        text += f"（从第 {record.recorded_from} 回合起记录）"
    return text


def place_text(place: int | None) -> str:
    return f"第 {place} 名" if place else "名次未知"


def _names(cards: Sequence[str], name: Namer) -> str:
    if len(cards) > LIST_NAMES:
        return f"{len(cards)} 张"
    return "、".join(name(c) for c in cards)


def actions_text(turn: TurnRecord, name: Namer) -> str:
    """一回合做了什么：升本、刷新、买（法术注明）、卖、打出、三连、冻结、技能。"""
    parts = []
    if turn.upgraded:
        parts.append(f"升本 {turn.upgrade_gold} 金")
    if turn.rerolls:
        parts.append(f"刷新 {turn.rerolls} 次")
    bought = [name(c) for c in turn.buys] + [f"{name(c)}（法术）" for c in turn.spells]
    if len(bought) > LIST_NAMES:
        spells = f"（含法术 {len(turn.spells)} 张）" if turn.spells else ""
        parts.append(f"买 {len(bought)} 张{spells}")
    elif bought:
        parts.append("买 " + "、".join(bought))
    if turn.sells:
        parts.append("卖 " + _names(turn.sells, name))
    if turn.plays:
        parts.append("打出 " + _names(turn.plays, name))
    if turn.triples:
        parts.append("三连 " + "、".join(name(c) for c in turn.triples))
    if turn.froze:
        parts.append("冻结")
    if turn.hero_power:
        parts.append("技能" if turn.hero_power == 1 else f"技能 {turn.hero_power} 次")
    return "；".join(parts) or "没有操作"


def board_text(turn: TurnRecord) -> str:
    return f"{len(turn.board_end)} 个 · 身材 {stats(turn.board_end)}"


def _opponent(combat: CombatRecord, name: Namer) -> str:
    return name(combat.opponent_hero) if combat.opponent_hero else "未知对手"


def combat_cell(combat: CombatRecord | None, name: Namer) -> str:
    """表格里的一格："负 −2（厄祖玛特）"、"胜 +5（洛卡拉）"、"残影（…）"。"""
    if combat is None:
        return "—"
    who = _opponent(combat, name)
    if combat.result == "win":
        return f"胜 +{combat.dealt}（{who}）"
    if combat.result == "loss":
        out = "，淘汰" if combat.health_after <= 0 else ""
        return f"负 −{combat.lost}{out}（{who}）"
    if combat.result == "ghost":
        return f"残影（{who}）"
    return f"平（{who}）"


def last_combat_text(record: GameRecord, name: Namer) -> str | None:
    """ "第 13 回合打厄祖玛特掉 18 血被淘汰"。"""
    if not record.combats:
        return None
    c = record.combats[-1]
    head = f"第 {c.turn} 回合打{_opponent(c, name)}"
    if c.result == "loss":
        return head + f"掉 {c.lost} 血" + ("被淘汰" if c.health_after <= 0 else "")
    if c.result == "win":
        return head + f"打出 {c.dealt} 血"
    return head + ("（残影）没掉血" if c.result == "ghost" else "打平")


def tribes_text(record: GameRecord) -> str:
    return "、".join(tribe_zh(t) for t in record.tribes) or "未知"


def advice_totals(record: GameRecord) -> tuple[int, int]:
    """(做了的, 建议过的)。"""
    advised = sum(t.advice.advised for t in record.turns if t.advice is not None)
    followed = sum(t.advice.followed for t in record.turns if t.advice is not None)
    return followed, advised


def data_line(record: GameRecord) -> str:
    """ "升本 第 2/4/5/7/8 回合（第 8 回合 6 本）· 刷新 13 次 · 没花的铸币 2 · 掉血 6 场 共 54
    · 建议采纳 15/18"。"""
    turns = record.turns
    ups = [t for t in turns if t.upgraded]
    if ups:
        parts = [
            "升本 第 " + "/".join(str(t.turn) for t in ups) + " 回合"
            f"（第 {ups[-1].turn} 回合 {ups[-1].tier_end} 本）"
        ]
    else:
        parts = ["没升过本"]
    parts.append(f"刷新 {sum(t.rerolls for t in turns)} 次")
    parts.append(f"没花的铸币 {sum(t.gold_end for t in turns)}")
    losses = [c for c in record.combats if c.result == "loss"]
    parts.append(f"掉血 {len(losses)} 场 共 {sum(c.lost for c in losses)}")
    followed, advised = advice_totals(record)
    if advised:
        parts.append(f"建议采纳 {followed}/{advised}")
    return " · ".join(parts)


def title_line(review: GameReview) -> str:
    record = review.record
    start = record.recorded_from
    since = f"，从第 {start} 回合起记录" if start is not None else ""
    return f"{review.hero_name} {place_text(record.place)}（{record.last_turn} 回合{since}）"


def terminal_lines(review: GameReview) -> list[str]:
    """规则版的终端块。"""
    lines = [f"  📝 复盘：{title_line(review)}"]
    if review.record.uncertain:
        lines.append(INDENT + UNCERTAIN)
    if not review.moments:
        lines.append(INDENT + NO_FLAGS)
    fixes: list[str] = []
    for i, moment in enumerate(review.moments):
        lines.append(f"{INDENT}{CIRCLED[i]} {moment.title}：{moment.facts}")
        if moment.fix:
            same = moment.fix in fixes
            fixes.append(moment.fix)
            lines.append(f"{SUB_INDENT}下次：{'（同上）' if same else moment.fix}")
    lines.append(f"{INDENT}下局重点：{review.focus}")
    lines.append(f"{INDENT}数据：{data_line(review.record)}")
    if review.habits is not None:
        lines.append(INDENT + review.habits.line())
    lines.append(INDENT + saved_text(review))
    return lines


def ai_terminal_lines(review: GameReview) -> list[str]:
    """AI 版的终端块；失败时一行。"""
    ai = review.ai
    if review.ai_state == "failed" or ai is None:
        reason = (review.ai_error or "未知原因").removeprefix("AI ")
        return [f"  🧠 AI 复盘失败：{reason}（文件里是规则版）"]
    seconds = f"（{review.ai_elapsed:.0f} 秒）" if review.ai_elapsed is not None else ""
    lines = [f"  🧠 AI 复盘{seconds}"]
    for i, moment in enumerate(ai.moments):
        lines.append(f"{INDENT}{CIRCLED[i]} 第 {moment.turn} 回合 {moment.title}：{moment.what}")
        lines.append(f"{SUB_INDENT}为什么要紧：{moment.why}")
        lines.append(f"{SUB_INDENT}下次：{moment.next}")
    if ai.good:
        lines.append(f"{INDENT}做得好：{ai.good}")
    lines.append(f"{INDENT}下局重点：{ai.focus}")
    lines.append(INDENT + saved_text(review, updated=True))
    return lines


def ai_moment_markdown(ai: AIReview) -> list[str]:
    """AI 版的"关键时刻"：what / 为什么要紧 / 下次 三行。"""
    lines = []
    for i, moment in enumerate(ai.moments, start=1):
        lines.append(f"{i}. **第 {moment.turn} 回合 {moment.title}** — {moment.what}")
        lines.append(f"   - 为什么要紧：{moment.why}")
        lines.append(f"   - 下次：{moment.next}")
    return lines


def review_markdown(review: GameReview, name: Namer) -> str:
    """有 AI 复盘时关键时刻、做得好和下局重点换成 AI 的（规则版的句子在"提醒（规则）"里）。"""
    ai = review.ai
    if ai is None:
        return markdown(review, name)
    return markdown(review, name, ai_moment_markdown(ai), ai.good, ai.focus)


def saved_text(review: GameReview, updated: bool = False) -> str:
    if review.save_error is not None:
        return f"复盘没能保存：{review.save_error}"
    return f"{'已更新' if updated else '已保存'} {review.shown_path}"


def choice_title(kind: str) -> str:
    try:
        return CHOICE_TITLES[ChoiceKind(kind)]
    except ValueError:
        return kind


def choice_text(pick: ChoicePick, name: Namer) -> str:
    """ "饰品：选 精神鞭笞者肖像（4 金）（AI 也选它）"。"""
    if pick.card_id is None:
        text = "没看到选了哪个"
    else:
        text = f"选 {name(pick.card_id)}"
        if pick.cost is not None:
            text += f"（{pick.cost} 金）"
        if pick.ai_card_id is not None:
            same = pick.ai_card_id == pick.card_id
            text += "（AI 也选它）" if same else f"（AI 推荐 {name(pick.ai_card_id)}）"
    return f"{choice_title(pick.kind)}：{text}"


def _choice_line(turn: int, pick: ChoicePick, name: Namer) -> str:
    return f"- 第 {turn} 回合 {choice_text(pick, name)}"


def _table(record: GameRecord, name: Namer) -> list[str]:
    lines = [
        "| 回合 | 本 | 铸币 | 做了什么 | 回合末场面 | 战斗 | 建议采纳 |",
        "|---|---|---|---|---|---|---|",
    ]
    for t in record.turns:
        tier = f"{t.tier_start}→{t.tier_end}" if t.upgraded else str(t.tier_end)
        gold = f"{t.gold_start}→{t.gold_end}"
        if t.gold_gained:
            gold += f"（+{t.gold_gained}）"
        advice = t.advice
        followed = f"{advice.followed}/{advice.advised}" if advice and advice.advised else "—"
        turn = f"{t.turn}（中途）" if t.partial else str(t.turn)
        cells = [
            turn,
            tier,
            gold,
            actions_text(t, name),
            board_text(t),
            combat_cell(record.combat(t.turn), name),
            followed,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def flag_line(flag: Flag) -> str:
    if flag.kind == "good_turn":
        return f"{flag.title}：{flag.facts}"
    return f"{flag.title}（{SEVERITY_NAMES[flag.severity]}）：{flag.facts}"


def moment_markdown(review: GameReview) -> list[str]:
    """规则版的"关键时刻"：事实 / 下次 两行。"""
    lines = []
    for i, moment in enumerate(review.moments, start=1):
        lines.append(f"{i}. **{moment.title}** — {moment.facts}")
        if moment.fix:
            lines.append(f"   - 下次：{moment.fix}")
    return lines or [NO_FLAGS]


def markdown(
    review: GameReview,
    name: Namer,
    moments: Sequence[str] | None = None,
    good: str = "",
    focus: str | None = None,
) -> str:
    """整份复盘。moments、good、focus：AI 版的关键时刻、做得好和下局重点（None 用规则版）。"""
    record = review.record
    head = f"# {review.hero_name} · {place_text(record.place)} · {review.when:%Y-%m-%d %H:%M}"
    summary = [f"本局种族：{tribes_text(record)}", turns_label(record, " 个回合")]
    last = last_combat_text(record, name)
    if last is not None:
        summary.append(f"最后一场：{last}")
    lines = [head, "", " · ".join(summary), ""]
    if record.uncertain:
        lines += [f"> {UNCERTAIN}", ""]
    lines += ["## 关键时刻", ""]
    lines += list(moments) if moments is not None else moment_markdown(review)
    lines.append("")
    if good:
        lines += [f"**做得好**：{good}", ""]
    lines += [f"**下局重点**：{focus if focus is not None else review.focus}", ""]
    if review.habits is not None:
        lines += [f"## 最近 {review.habits.games} 局", review.habits.sentence(), ""]
    lines += ["## 逐回合", "", *_table(record, name), ""]
    lines += [f"- {data_line(record)}", ""]
    lines += ["## 提醒（规则）", ""]
    lines += [f"- {flag_line(f)}" for f in review.flags] or [NO_FLAGS]
    lines.append("")
    picks = [(t.turn, c) for t in record.turns for c in t.choices]
    if picks:
        lines += ["## 选项", "", *(_choice_line(turn, c, name) for turn, c in picks), ""]
    lines.append(VERSION_MARK)
    return "\n".join(lines) + "\n"


def safe_name(text: str) -> str:
    return _UNSAFE.sub("_", text)


def review_path(directory: Path, when: datetime, hero: str, place: int | None) -> Path:
    """<dir>/<YYYY-MM-DD>_<HHMM>_<英雄>_第<N>名.md；同名文件已存在就加 _2、_3。"""
    rank = f"第{place}名" if place else "未完"
    stem = f"{when:%Y-%m-%d_%H%M}_{safe_name(hero)}_{rank}"
    path = directory / f"{stem}.md"
    number = 2
    while path.exists():
        path = directory / f"{stem}_{number}.md"
        number += 1
    return path


def write_text(path: Path, text: str) -> None:
    """失败时抛出 OSError。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def history_entry(review: GameReview) -> dict:
    record = review.record
    turns = record.turns
    tier_at = {str(n): t.tier_end for n in CHECKPOINTS if (t := record.turn(n)) is not None}
    board_at = {str(n): len(t.board_end) for n in BOARD_TURNS if (t := record.turn(n)) is not None}
    followed, advised = advice_totals(record)
    return {
        "time": review.when.isoformat(timespec="seconds"),
        "hero": record.hero_card_id,
        "hero_name": review.hero_name,
        "place": record.place,
        "turns": record.last_turn,
        "tier_at": tier_at,
        "upgrade_turns": [t.turn for t in turns if t.upgraded],
        "rerolls": sum(t.rerolls for t in turns),
        "max_rerolls": max((t.rerolls for t in turns), default=0),
        "gold_unspent": sum(t.gold_end for t in turns),
        "hp_lost": sum(c.lost for c in record.combats),
        "loss_combats": sum(1 for c in record.combats if c.result == "loss"),
        "board_at": board_at,
        "triples": sum(len(t.triples) for t in turns),
        "advised": advised,
        "followed": followed,
        "plan_comps": list(record.plan_comps),  # 整局计划走过的目标流派
        "plan_changes": record.plan_changes,  # 整局计划改了几次
        "flags": flag_kinds(review.flags),
        "file": review.shown_path,
    }


def review_game(
    record: GameRecord,
    rules: RulesConfig,
    name: Namer,
    when: datetime,
    history: Sequence[dict] = (),
    history_games: int = 10,
) -> GameReview:
    """规则版复盘（还没保存）：提醒、关键时刻、下局重点；最近几局的习惯算上这一局。"""
    flags = find_flags(record, rules, name)
    lost = sum(c.lost for c in record.combats)
    moments = pick_moments(flags, turn_count(record), winner=record.place == 1 and lost == 0)
    hero = name(record.hero_card_id) if record.hero_card_id else "未知英雄"
    review = GameReview(record, flags, moments, rules_focus(moments), hero, when)
    habit = habits([*history, history_entry(review)], history_games)
    return replace(review, habits=habit, focus=final_focus(review.focus, flags, habit))


def shown(path: Path, base: Path | None) -> str:
    """项目目录下的文件写相对路径，其他写完整路径。"""
    if base is not None:
        try:
            return str(path.relative_to(base))
        except ValueError:
            pass
    return str(path)


def save_review(
    review: GameReview, directory: Path, name: Namer, base: Path | None = None
) -> GameReview:
    """写 Markdown 文件；失败（目录建不了、没有权限）只记日志，原因放进 save_error。"""
    try:
        path = review_path(directory, review.when, review.hero_name, review.record.place)
        write_text(path, markdown(review, name))
    except OSError as exc:
        log.warning("保存复盘失败", exc_info=True)
        return replace(review, save_error=exc.strerror or str(exc) or type(exc).__name__)
    return replace(review, path=path, shown_path=shown(path, base), save_error=None)
