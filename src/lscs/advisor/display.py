"""把建议格式化成终端文字；DisplayGroup 把建议同时发给多个显示端（终端、悬浮面板）。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from lscs.advisor.models import Action, Advice, Expired, HeroAdvice, Lane, Plan
from lscs.state.models import GameState

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
Output = Callable[[str], None]
CIRCLED = "①②③④⑤⑥⑦⑧⑨"
INDENT = "          "  # 让多步方案的后续行与第一步对齐
ICONS: dict[Lane, str] = {"fast": "🤖 ", "deep": "🧠 ", "hero": "🤖 "}
EXPIRED_LABELS: dict[Expired, str] = {
    "refreshed": "操作已过期（商店刷新了），只看思路",
    "invalid": "操作不可用，只看思路",
    "changed": "操作已过期，只看思路",
}


def _print(line: str) -> None:
    print(line, flush=True)


def _describe_entity(entity_id: int, state: GameState, name: Namer) -> str:
    """场面、商店、手牌里的实体：随从带身材；都找不到时用编号。"""
    me = state.me
    cards = [*(me.board if me else ()), *state.shop, *(me.hand if me else ())]
    card = next((c for c in cards if c.entity_id == entity_id), None)
    if card is None:
        return f"#{entity_id}"
    if getattr(card, "card_type", "MINION") == "MINION" and card.atk is not None:
        return f"{name(card.card_id)} {card.atk}/{card.health}"
    return name(card.card_id)


def describe_action(action: Action, state: GameState, name: Namer, reasons: bool = True) -> str:
    me = state.me
    match action.type:
        case "buy":
            item = next((s for s in state.shop if s.entity_id == action.entity_id), None)
            if item is None:
                what = f"#{action.entity_id}"
            elif item.card_type == "MINION":
                what = f"{name(item.card_id)} {item.atk}/{item.health}"
            else:
                price = f"，{item.cost} 金" if item.cost is not None else ""
                what = f"{name(item.card_id)}（法术{price}）"
            text = f"买 {what}"
        case "sell":
            board = me.board if me is not None else ()
            m = next((m for m in board if m.entity_id == action.entity_id), None)
            what = f"{name(m.card_id)} {m.atk}/{m.health}" if m else f"#{action.entity_id}"
            text = f"卖 {what}"
        case "upgrade":
            tier = me.tavern_tier + 1 if me is not None else "?"
            text = f"升本 → {tier} 本"
        case "hero_power":
            hp = me.hero_power if me is not None else None
            text = "使用英雄技能" + (f" {name(hp.card_id)}" if hp is not None else "")
            if hp is not None and hp.needs_target is not False and action.entity_id is not None:
                text += f" → {_describe_entity(action.entity_id, state, name)}"
        case "refresh":
            text = "刷新"
        case _:
            text = "冻结"
    return f"{text}（{action.reason}）" if action.reason and reasons else text


def format_plan(plan: Plan, state: GameState, name: Namer, reasons: bool = True) -> list[str]:
    """第一行不带前缀；多步时每步一行。reasons=False 时不带理由（悬浮面板用）。"""
    if not plan.actions:
        return [f"无需操作（{plan.summary}）" if plan.summary and reasons else "无需操作"]
    return [
        f"{CIRCLED[i] if i < len(CIRCLED) else i + 1} {describe_action(a, state, name, reasons)}"
        for i, a in enumerate(plan.actions)
    ]


def deep_wait_hint(thinking: str) -> str:
    """深度分析大约要等多久（终端提示和启动时的说明共用）。"""
    return "约 40 秒" if thinking == "high" else "约 20 秒"


def one_line(text: str) -> str:
    """AI 写的多行文字合成一行。"""
    return " ".join(text.split())


def thought_lines(plan: Plan, summary: bool = True, extra: tuple[str, ...] = ()) -> list[str]:
    """AI 方案下面的"分析"一行（没有分析时用"思路"），以及"方向 / 要找"一行。"""
    lines = []
    if plan.analysis:
        lines.append(f"分析：{one_line(plan.analysis)}")
    elif plan.summary and summary:
        lines.append(f"思路：{one_line(plan.summary)}")
    tail = []
    if plan.direction:
        tail.append(f"方向：{plan.direction}")
    if plan.look_for:
        tail.append("要找：" + "、".join(plan.look_for))
    tail += extra
    if tail:
        lines.append("   ".join(tail))
    return lines


def hero_name(state: GameState, entity_id: int | None, name: Namer) -> str:
    """备选英雄的名字；不在当前备选里时用编号。"""
    select = state.hero_select
    offered = select.offered if select is not None else ()
    hero = next((h for h in offered if h.entity_id == entity_id), None)
    return name(hero.card_id) if hero is not None else f"#{entity_id}"


def hero_comment_lines(state: GameState, advice: HeroAdvice, name: Namer) -> list[str]:
    """AI 对每个英雄的点评，按备选顺序，跳过没有点评的（推荐的那个有点评也列出）。"""
    comments = {c.entity_id: c.text for c in advice.comments if c.text}
    select = state.hero_select
    offered = select.offered if select is not None else ()
    return [
        f"{name(h.card_id)}：{comments[h.entity_id]}" for h in offered if h.entity_id in comments
    ]


class AdviceSink(Protocol):
    """AdviceRunner 输出建议的对象：终端（AdviceDisplay）、悬浮面板或 DisplayGroup。"""

    def show_rules(self, state: GameState, advice: Advice) -> None: ...
    def rules_error(self, hero: bool = False) -> None: ...
    def ai_pending(self, lane: Lane = "fast") -> None: ...
    def ai_text(self, text: str, lane: Lane = "fast") -> None: ...
    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None: ...
    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None: ...
    def ai_summary(self, calls: int, input_tokens: int, output_tokens: int) -> None: ...


class AdviceDisplay:
    def __init__(self, name: Namer, out: Output = _print, deep_thinking: str = "low") -> None:
        self._name = name
        self._out = out
        self._deep_pending = f"深度分析中…（{deep_wait_hint(deep_thinking)}）"
        self._last_rules: tuple | None = None

    def _block(self, label: str, lines: list[str]) -> None:
        self._out(f"  {label}{lines[0]}")
        for line in lines[1:]:
            self._out(f"{INDENT}{line}")

    def show_rules(self, state: GameState, advice: Advice) -> None:
        key = (advice.turn, advice.plan.signature())
        if key == self._last_rules:
            return
        self._last_rules = key
        lines = format_plan(advice.plan, state, self._name)
        if advice.plan.direction:
            lines[-1] += f"   方向：{advice.plan.direction}"
        if state.uncertain:
            lines.append("⚠ 状态可能不准")
        self._block("📋 规则：", lines)

    def rules_error(self, hero: bool = False) -> None:
        """hero：选英雄的规则建议出错。"""
        self._out("  📋 选英雄规则建议出错" if hero else "  📋 本回合规则建议出错")

    def ai_pending(self, lane: Lane = "fast") -> None:
        self._out(("  🧠 " + self._deep_pending) if lane == "deep" else "  🤖 AI 分析中…")

    def ai_text(self, text: str, lane: Lane = "fast") -> None:
        """AI 的错误、提示、过期等一行消息。"""
        self._out(f"  {ICONS[lane]}{text}")

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        plan, deep = advice.plan, advice.lane == "deep"
        icon = ICONS[advice.lane]
        if advice.expired is not None:
            # 冒号而不是括号：原因里本身带括号，不嵌套
            self._block(icon, [f"深度分析：{EXPIRED_LABELS[advice.expired]}", *thought_lines(plan)])
            return
        if advice.elapsed is None:
            seconds = "?"
        else:  # 深度分析要等十几秒，不用精确到 0.1 秒
            seconds = f"{advice.elapsed:.0f} 秒" if deep else f"{advice.elapsed:.1f} 秒"
        if agrees:
            who = "深度分析" if deep else "AI "
            self._block(icon, [f"✓ {who}同意规则建议（{seconds}）", *thought_lines(plan)])
            return
        stale = (f"（{advice.stale_note}）",) if advice.stale_note else ()
        lines = format_plan(plan, state, self._name)
        lines += thought_lines(plan, summary=bool(plan.actions), extra=stale)
        label = f"深度分析（{seconds}）：" if deep else f"AI（{seconds}）："
        self._block(icon + label, lines)

    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None:
        """选英雄建议。规则：一行；AI：推荐和理由一行，下面每个英雄的点评各一行。
        agrees：AI 和规则推荐的是同一个英雄。"""
        pick = hero_name(state, advice.pick, self._name)
        if advice.source == "rules":
            text = f"选 {pick}（{advice.reason}）" if advice.pick is not None else advice.reason
            self._out(f"  📋 规则：{text}")
            return
        seconds = "?" if advice.elapsed is None else f"{advice.elapsed:.1f} 秒"
        if agrees:
            first = f"✓ AI 也选 {pick}（{seconds}）：{advice.reason}"
        else:
            first = f"AI（{seconds}）：选 {pick} —— {advice.reason}"
        self._block("🤖 ", [first, *hero_comment_lines(state, advice, self._name)])

    def ai_summary(self, calls: int, input_tokens: int, output_tokens: int) -> None:
        self._out(
            f"本局 AI 调用 {calls} 次，输入 {input_tokens / 1000:.1f} 千、"
            f"输出 {output_tokens / 1000:.1f} 千 token"
        )


class DisplayGroup:
    """把每条建议依次转发给多个显示端；一个显示端出错只记日志，不影响其他显示端。"""

    def __init__(self, *displays: AdviceSink) -> None:
        self._displays = displays

    def _each(self, method: str, *args: object) -> None:
        for display in self._displays:
            try:
                getattr(display, method)(*args)
            except Exception:
                log.warning("显示建议时出错（%s）", method, exc_info=True)

    def show_rules(self, state: GameState, advice: Advice) -> None:
        self._each("show_rules", state, advice)

    def rules_error(self, hero: bool = False) -> None:
        self._each("rules_error", hero)

    def ai_pending(self, lane: Lane = "fast") -> None:
        self._each("ai_pending", lane)

    def ai_text(self, text: str, lane: Lane = "fast") -> None:
        self._each("ai_text", text, lane)

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        self._each("show_ai", state, advice, agrees)

    def show_hero(self, state: GameState, advice: HeroAdvice, agrees: bool = False) -> None:
        self._each("show_hero", state, advice, agrees)

    def ai_summary(self, calls: int, input_tokens: int, output_tokens: int) -> None:
        self._each("ai_summary", calls, input_tokens, output_tokens)
