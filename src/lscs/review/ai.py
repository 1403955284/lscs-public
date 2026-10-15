"""AI 复盘（对局复盘设计文档第 6 节）：把规则算出的事实写成新手看得懂的几段话。

每局打完调 1 次（不合格时再改正 1 次），用单独的系统提示，不改招募的系统提示和记忆、不占每局的
调用预算；调用记录照常写进 recordings/llm-*.jsonl（lane = "review"）。AI 写的每一条都要核对：
回合是本局打过的、没提到本局没出现过的牌；不合格、调用失败或超时都退回规则版。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from lscs.advisor.names import minion_label
from lscs.cards.db import GOLDEN_MARKS, CardDB
from lscs.llm.parse import EmptyResponse, ParseError, _cut, _json_object
from lscs.llm.prompt import REPAIR_PICK
from lscs.llm.provider import LLMError, LLMProvider, LLMResult, Thinking, Turn
from lscs.llm.record import Recorder
from lscs.review.flags import Flag
from lscs.review.record import CombatRecord, GameRecord, TurnRecord, stats
from lscs.review.report import (
    Habits,
    actions_text,
    advice_totals,
    choice_text,
    flag_line,
    last_combat_text,
    place_text,
    tribes_text,
    turns_label,
)

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
MAX_TOKENS = 4000
# 提示词里要求 title 15 字、what / why / next 60 字、focus 30 字以内；AI 常常多写几个字，
# 放宽到约 1.5 倍（focus 50 字）才处理，而且只在逗号、分号、句号处截断，不截半句话
TITLE_CHARS = 22
TEXT_CHARS = 90  # what、why、next、good
FOCUS_CHARS = 50
GOOD_CHARS = 90
CLAUSE_ENDS = "，；。,;"
SHORT_GAME = 5  # 对局不到这么多回合时只要 1 条
REPAIR_MIN_SECONDS = 3.0  # 改正要发出时离超时至少还有这么多秒
EMPTY = "AI 返回了空内容"
INVALID_AFTER_REPAIR = "AI 改正后仍不合格"

REVIEW_EXAMPLE = {
    "moments": [
        {
            "turn": 8,
            "title": "连续升本没补场面",
            "what": "第 7、8 回合只升本没买随从，场上一直 5 个小畸变",
            "why": "6 本时场面没跟上，第 8、9 回合连掉 30 血",
            "next": "升本的回合也买一张随从；场上不到 6 个先补场面",
        }
    ],
    "focus": "升本和补场面要一起做：每回合至少买一张随从",
    "good": "第 11 回合凑出金色蚀脑怪，之后连赢两场",
}

# 对局复盘设计文档第 6.2 节的原文
REVIEW_SYSTEM_TEMPLATE = """你是《炉石传说》酒馆战棋的教练，正在给一位 6000 分以下的新手写一局打完后的复盘。请用简体中文，具体、直接、口语化，像教练当面说话。

【怎么写】
- 只根据下面给的事实写，不要编造没出现过的牌、回合或数字；提到牌就用事实里的卡名。
- 从"提醒"里挑 2~3 个最影响名次的时刻（事实里已经按要紧程度排好，一般按它来；打得好的也可以写 1 条）。每个时刻写清楚：那回合做了什么、为什么要紧（和之后掉血、名次的关系）、下次遇到同样情况怎么做。
- 不要逐回合复述，不要讲大道理，每一句都要能落实到操作（买什么、什么时候升本、刷几次）。
- 新手看得懂：不用缩写和黑话。

只输出一个 json 对象，格式：
{example}
moments 2~3 条（对局不到 5 回合时 1 条），每条的 turn 必须是事实里出现过的回合；title 15 字以内；what、why、next 各 60 字以内，一行；focus 是下一局最该注意的一件事，30 字以内；good 是这局做得好的一点（没有就空字符串）。

【攻略要点】
{guide}"""  # noqa: E501


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Moment(_Frozen):
    turn: int
    title: str
    what: str
    why: str
    next: str


class AIReview(_Frozen):
    moments: tuple[Moment, ...]
    focus: str
    good: str = ""


def build_review_system_prompt(guide: str) -> str:
    return REVIEW_SYSTEM_TEMPLATE.format(
        example=json.dumps(REVIEW_EXAMPLE, ensure_ascii=False), guide=guide
    )


# ---- 事实消息 ----


def _flag_line(flag: Flag, starred: bool) -> str:
    return ("★ " if starred else "  ") + flag_line(flag)


def _gold_text(turn: TurnRecord, name: Namer) -> str:
    extra = []
    for pick in turn.choices:
        if pick.kind == "trinket" and pick.card_id and pick.cost:
            extra.append(f"饰品 {name(pick.card_id)} {pick.cost} 金")
    if turn.gold_gained:
        extra.append(f"回合中 +{turn.gold_gained}")
    text = f"铸币 {turn.gold_start}→{turn.gold_end}"
    return text + (f"（{'；'.join(extra)}）" if extra else "")


def _board_text(turn: TurnRecord, name: Namer) -> str:
    board = turn.board_end
    head = f"场面 {len(board)} 个（身材 {stats(board)}）"
    if not board:
        return head
    return head + "：" + "、".join(f"{minion_label(m, name)} {m.atk}/{m.health}" for m in board)


def _combat_text(combat: CombatRecord | None, name: Namer) -> str | None:
    if combat is None:
        return None
    who = name(combat.opponent_hero) if combat.opponent_hero else "未知对手"
    if combat.ghost:
        who += "的残影"
    if combat.opponent_minions:
        who += f"，{combat.opponent_minions} 个随从、身材 {combat.opponent_stats}"
    elif not combat.ghost:
        who += "，没看到阵容"
    if combat.result == "win":
        return f"战斗 胜 打出 {combat.dealt}（{who}）"
    if combat.result == "loss":
        out = "，淘汰" if combat.health_after <= 0 else ""
        return f"战斗 负 掉 {combat.lost}{out}（{who}）"
    if combat.result == "ghost":
        return f"战斗 打残影 没掉血（{who}）"
    return f"战斗 平（{who}）"


def _advice_text(turn: TurnRecord, name: Namer) -> str | None:
    advice = turn.advice
    if advice is None:
        return None
    parts = []
    if advice.rules_actions:
        parts.append("规则 " + "、".join(_action_name(a, c, name) for a, c in advice.rules_actions))
    if advice.deep_summary:
        parts.append(f'深度 "{" ".join(advice.deep_summary.split())}"')
    if advice.plan_comp:
        stage = f"/{advice.plan_stage}" if advice.plan_stage else ""
        parts.append(f'计划 "{advice.plan_comp}{stage}"')
    if advice.advised:
        parts.append(f"采纳 {advice.followed}/{advice.advised}")
    return "建议：" + "；".join(parts) if parts else None


ACTION_NAMES = {
    "buy": "买",
    "sell": "卖",
    "upgrade": "升本",
    "refresh": "刷新",
    "freeze": "冻结",
    "hero_power": "技能",
}


def _action_name(action: str, card_id: str | None, name: Namer) -> str:
    verb = ACTION_NAMES.get(action, action)
    return f"{verb}{name(card_id)}" if card_id else verb


def turn_line(record: GameRecord, turn: TurnRecord, name: Namer) -> str:
    """【逐回合】的一行。"""
    tier = f"{turn.tier_start}→{turn.tier_end} 本" if turn.upgraded else f"{turn.tier_end} 本"
    parts = [
        f"第 {turn.turn} 回合" + ("（中途开始记录）" if turn.partial else ""),
        tier,
        _gold_text(turn, name),
        actions_text(turn, name),
    ]
    choices = [choice_text(c, name) for c in turn.choices if c.card_id]
    if choices:
        parts.append("选择：" + "；".join(choices))
    parts.append(_board_text(turn, name))
    combat = _combat_text(record.combat(turn.turn), name)
    if combat is not None:
        parts.append(combat)
    advice = _advice_text(turn, name)
    if advice is not None:
        parts.append(advice)
    return " | ".join(parts)


def build_review_message(
    record: GameRecord,
    flags: Sequence[Flag],
    moments: Sequence[Flag],
    name: Namer,
    history: Habits | None = None,
) -> str:
    """对局概况、提醒（按要紧程度，★ 是建议写进复盘的）、逐回合、建议采纳、最近几局。"""
    hero = name(record.hero_card_id) if record.hero_card_id else "未知英雄"
    head = [
        f"对局：{hero} | {place_text(record.place)} | {turns_label(record, ' 个回合')}"
        f" | 本局种族：{tribes_text(record)}"
    ]
    last = last_combat_text(record, name)
    if last is not None:
        head.append(f"最后：{last}")
    ordered = sorted(flags, key=lambda f: (-f.severity, f.turns[0]))
    lines = ["【提醒】（按要紧程度排好；★ 是建议写进复盘的）"]
    lines += [_flag_line(f, f in moments) for f in ordered] or ["（没有触发提醒）"]
    turns = ["【逐回合】本 = 酒馆等级；铸币 开始→剩余；战斗写结果和双方阵容规模"]
    turns += [turn_line(record, t, name) for t in record.turns]
    blocks = ["\n".join(head), "\n".join(lines), "\n".join(turns)]
    followed, advised = advice_totals(record)
    if advised:
        missed = []
        for t in record.turns:
            for card in t.advice.missed_buys if t.advice is not None else ():
                label = name(card)
                if label not in missed:
                    missed.append(label)
        text = f"【建议采纳】规则和 AI 一共建议 {advised} 个动作，做了 {followed} 个"
        if missed:
            text += "；建议买而没买：" + "、".join(missed)
        blocks.append(text)
    if history is not None:
        blocks.append(f"【最近 {history.games} 局】{history.sentence()}")
    return "\n\n".join(blocks)


# ---- 解析与校验 ----


def _text(raw: object) -> str:
    return " ".join(raw.split()) if isinstance(raw, str) else ""


def _int(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    return None


def fit(text: str, limit: int) -> str:
    """超出 limit 个字时截到 limit 以内最后一个逗号、分号或句号之前；一个都没有才硬截加"…"。"""
    if len(text) <= limit:
        return text
    cut = max(text.rfind(mark, 0, limit) for mark in CLAUSE_ENDS)
    # 标点太靠前时（不到一半）截出来的话太短，宁可硬截
    return text[:cut] if cut >= limit // 2 else _cut(text, limit)


def _strip_golden(card_name: str) -> str:
    for mark in GOLDEN_MARKS:
        card_name = card_name.removesuffix(mark)
    return card_name.strip()


SHORT_NAME = 2  # 这么短的卡名（两个字）只在前后不挨着别的字时才算提到


def _mentions(text: str, card_name: str) -> bool:
    if len(card_name) > SHORT_NAME:
        return card_name in text
    return re.search(rf"(?<!\w){re.escape(card_name)}(?!\w)", text) is not None


def unknown_cards(text: str, record: GameRecord, cards: CardDB) -> list[str]:
    """text 里提到的、本局没出现过的卡池随从的卡名。是某张出现过的牌名或英雄名（自己的和对手的）
    的一部分时不算（"小鬼"和"粗鄙小鬼"）；两个字的卡名只在单独出现时才算。"""
    seen = {_strip_golden(cards.name(card)) for card in record.seen_cards}
    heroes = {record.hero_card_id, *(c.opponent_hero for c in record.combats)}
    known = seen | {cards.name(h) for h in heroes if h}
    mentioned = [s for s in known if s and s in text]
    found: list[str] = []
    for card in cards.pool_minions():
        card_name = _strip_golden(card.name)
        if not card_name or card_name in known or card_name in found:
            continue
        if not _mentions(text, card_name) or any(card_name in other for other in mentioned):
            continue
        found.append(card_name)
    return found


def parse_review(text: str, record: GameRecord, cards: CardDB | None = None) -> AIReview:
    """不合格时抛出 ParseError（原因原样发回去改正）；空内容抛出 EmptyResponse。
    长度超出放宽后的上限时在逗号、分号、句号处截断（不算不合格）。
    cards 为 None 时不查编造的卡名。"""
    data = _json_object(text)
    raw = data.get("moments")
    if not isinstance(raw, list):
        raise ParseError("缺少 moments 列表")
    short = record.last_turn < SHORT_GAME
    low = 1 if short else 2
    if not low <= len(raw) <= 3:
        raise ParseError(f"moments 应该是 {low}~3 条，现在是 {len(raw)} 条")
    played = {t.turn for t in record.turns}
    moments = []
    for i, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ParseError(f"第 {i} 条 moments 不是对象")
        turn = _int(item.get("turn"))
        if turn is None or turn not in played:
            first, last = min(played, default=0), max(played, default=0)
            raise ParseError(f"第 {i} 条的 turn 不是本局打过的回合（{first}~{last}）")
        fields = {}
        for key, limit in (("title", TITLE_CHARS), ("what", TEXT_CHARS), ("why", TEXT_CHARS)):
            fields[key] = fit(_text(item.get(key)), limit)
        fields["next"] = fit(_text(item.get("next")), TEXT_CHARS)
        missing = [key for key, value in fields.items() if not value]
        if missing:
            raise ParseError(f"第 {i} 条缺少 {'、'.join(missing)}")
        moments.append(Moment(turn=turn, **fields))
    focus = fit(_text(data.get("focus")), FOCUS_CHARS)
    if not focus:
        raise ParseError("缺少 focus")
    review = AIReview(
        moments=tuple(moments), focus=focus, good=fit(_text(data.get("good")), GOOD_CHARS)
    )
    if cards is not None:
        written = " ".join(
            [f"{m.title} {m.what} {m.why} {m.next}" for m in review.moments]
            + [review.focus, review.good]
        )
        unknown = unknown_cards(written, record, cards)
        if unknown:
            raise ParseError(f"提到了本局没出现过的牌：{'、'.join(unknown[:3])}")
    return review


# ---- 调用 ----


@dataclass(frozen=True)
class ReviewResult:
    review: AIReview | None
    error: str | None
    elapsed: float
    repaired: bool = False


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class ReviewCall:
    """一次 AI 复盘的参数。"""

    provider: LLMProvider
    model: str
    thinking: Thinking
    timeout: float
    recorder: Recorder | None = None
    max_tokens: int = MAX_TOKENS
    repair: bool = True  # 不合格时让它改正一次（[llm] repair_invalid）


def run_review(
    call: ReviewCall,
    system: str,
    user: str,
    record: GameRecord,
    cards: CardDB | None,
    clock: Callable[[], float],
) -> ReviewResult:
    """在工作线程里调用：调一次，不合格时改正一次（改正不开思考）。不抛异常。"""
    start = clock()
    deadline = start + call.timeout
    turns: tuple[Turn, ...] = ()
    sent, thinking, repair = user, call.thinking, False

    def record_call(result: LLMResult | None, outcome: str) -> None:
        if call.recorder is None:
            return
        call.recorder.write(
            {
                "request_id": None,
                "lane": "review",
                "turn": record.last_turn,
                "state_seq": None,
                "model": call.model,
                "thinking": thinking,
                "system_sha1": _sha1(system),
                "user": sent,
                "response": result.text if result else None,
                "elapsed": round(result.elapsed, 3) if result else None,
                "input_tokens": result.input_tokens if result else 0,
                "cached_tokens": result.cached_tokens if result else 0,
                "output_tokens": result.output_tokens if result else 0,
                "outcome": outcome,
                "repair": repair,
            }
        )

    while True:
        try:
            result = call.provider.complete_json(
                system,
                user,
                max(deadline - clock(), 1.0),
                thinking=thinking,
                max_tokens=call.max_tokens,
                turns=turns,
            )
        except LLMError as exc:
            record_call(None, f"失败：{exc}")
            return ReviewResult(None, str(exc), clock() - start, repair)
        except Exception:
            log.warning("AI 复盘调用出错", exc_info=True)
            record_call(None, "失败：程序出错")
            return ReviewResult(None, "AI 调用出错", clock() - start, repair)
        try:
            review = parse_review(result.text, record, cards)
        except EmptyResponse:
            record_call(result, "空内容")
            return ReviewResult(None, EMPTY, clock() - start, repair)
        except ParseError as exc:
            problem = str(exc)
            record_call(result, f"无效：{problem}")
            if repair or not call.repair or clock() > deadline - REPAIR_MIN_SECONDS:
                error = INVALID_AFTER_REPAIR if repair else f"回答不合格（{problem}）"
                return ReviewResult(None, error, clock() - start, repair)
            sent = REPAIR_PICK.format(problem=problem)
            turns = (("assistant", result.text), ("user", sent))
            thinking, repair = "disabled", True  # 思考在第一次回答里做完了，改正只修格式和事实
            continue
        record_call(result, "合法")
        return ReviewResult(review, None, clock() - start, repair)
