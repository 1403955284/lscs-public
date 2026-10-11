"""把状态更新格式化成便于人工检查的文本时间线。"""

from __future__ import annotations

from collections.abc import Callable

from lscs.state.models import Event, EventType, GameState, Minion, TribesConfidence

Namer = Callable[[str], str]
KEY_EVENTS = (
    EventType.GAME_STARTED,
    EventType.HERO_CHOSEN,
    EventType.RECRUIT_STARTED,
    EventType.COMBAT_STARTED,
    EventType.COMBAT_ENDED,
    EventType.GAME_ENDED,
)


def _identity(card_id: str) -> str:
    return card_id


def _minion(m: Minion, name: Namer) -> str:
    golden = "(金)" if m.golden and "(金)" not in name(m.card_id) else ""
    return f"{name(m.card_id)}{golden} {m.atk}/{m.health}"


def _list(items: list[str]) -> str:
    return "、".join(items) if items else "-"


def describe(
    state: GameState, events: list[Event], name: Namer = _identity, verbose: bool = False
) -> list[str]:
    lines: list[str] = []
    for event in events:
        if not verbose and event.type not in KEY_EVENTS:
            continue
        prefix = f"[T{event.turn:>2}]"
        me = state.me
        match event.type:
            case EventType.GAME_STARTED:
                lines.append(f"{prefix} 对局开始")
            case EventType.HERO_CHOSEN:
                lines.append(f"{prefix} 选择英雄 {name(str(event.data['hero']))}")
            case EventType.RECRUIT_STARTED if me is not None:
                tribes = _list(sorted(state.tribes))
                if state.tribes_confidence == TribesConfidence.INFERRING:
                    tribes += "（推断中）"
                lines.append(
                    f"{prefix} 招募开始 | 铸币 {me.gold} | 酒馆 {me.tavern_tier} 本"
                    f" | 升本 {me.upgrade_cost if me.upgrade_cost is not None else '-'}"
                    f" | 刷新 {me.refresh_cost if me.refresh_cost is not None else '-'}"
                    f" | 血量 {me.health}+{me.armor} | 种族 {tribes}"
                )
                shop = [
                    f"{name(s.card_id)}{' ❄' if s.frozen else ''}"
                    + (f" {s.atk}/{s.health}" if s.card_type == "MINION" else "")
                    for s in state.shop
                ]
                lines.append(f"      商店: {_list(shop)}")
                lines.append(f"      场面: {_list([_minion(m, name) for m in me.board])}")
                lines.append(f"      手牌: {_list([name(h.card_id) for h in me.hand])}")
            case EventType.COMBAT_STARTED:
                lines.append(
                    f"{prefix} 战斗开始 对手 {event.data.get('opponent')}"
                    f" {name(str(event.data.get('hero')))}"
                )
            case EventType.COMBAT_ENDED:
                opp = state.opponents.get(event.data.get("opponent"))  # type: ignore[arg-type]
                board = [_minion(m, name) for m in opp.last_board] if opp else []
                health = me.health if me is not None else "-"
                lines.append(f"{prefix} 战斗结束 我方血量 {health} | 对手阵容: {_list(board)}")
            case EventType.GAME_ENDED:
                lines.append(f"{prefix} 对局结束 名次 {event.data.get('place')}")
            case _:
                lines.append(f"{prefix} {event.type.value}")
    if state.uncertain and lines:
        lines.append("      ⚠ 日志解析出错过，状态可能不准")
    return lines
