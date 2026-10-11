"""把建议格式化成终端文字；DisplayGroup 把建议同时发给多个显示端（终端、悬浮面板）。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from lscs.advisor.models import Action, Advice, Plan
from lscs.state.models import GameState

log = logging.getLogger(__name__)

Namer = Callable[[str], str]
Output = Callable[[str], None]
CIRCLED = "①②③④⑤⑥⑦⑧⑨"
INDENT = "          "  # 让多步方案的后续行与第一步对齐


def _print(line: str) -> None:
    print(line, flush=True)


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


class AdviceSink(Protocol):
    """AdviceRunner 输出建议的对象：终端（AdviceDisplay）、悬浮面板或 DisplayGroup。"""

    def show_rules(self, state: GameState, advice: Advice) -> None: ...
    def rules_error(self) -> None: ...
    def ai_pending(self) -> None: ...
    def ai_text(self, text: str) -> None: ...
    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None: ...
    def ai_summary(self, calls: int, input_tokens: int, output_tokens: int) -> None: ...


class AdviceDisplay:
    def __init__(self, name: Namer, out: Output = _print) -> None:
        self._name = name
        self._out = out
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

    def rules_error(self) -> None:
        self._out("  📋 本回合规则建议出错")

    def ai_pending(self) -> None:
        self._out("  🤖 AI 分析中…")

    def ai_text(self, text: str) -> None:
        """AI 的错误、提示、过期等一行消息。"""
        self._out(f"  🤖 {text}")

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        seconds = f"{advice.elapsed:.1f} 秒" if advice.elapsed is not None else "?"
        if agrees:
            self._out(f"  🤖 ✓ AI 同意规则建议（{seconds}）")
            return
        lines = format_plan(advice.plan, state, self._name)
        extra = []
        if advice.plan.summary:
            extra.append(f"思路：{advice.plan.summary}")
        if advice.plan.direction:
            extra.append(f"方向：{advice.plan.direction}")
        if advice.stale_note:
            extra.append(f"（{advice.stale_note}）")
        if extra:
            lines.append("   ".join(extra))
        self._block(f"🤖 AI（{seconds}）：", lines)

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

    def rules_error(self) -> None:
        self._each("rules_error")

    def ai_pending(self) -> None:
        self._each("ai_pending")

    def ai_text(self, text: str) -> None:
        self._each("ai_text", text)

    def show_ai(self, state: GameState, advice: Advice, agrees: bool) -> None:
        self._each("show_ai", state, advice, agrees)

    def ai_summary(self, calls: int, input_tokens: int, output_tokens: int) -> None:
        self._each("ai_summary", calls, input_tokens, output_tokens)
