"""把状态更新格式化成便于人工检查的文本时间线。"""

from __future__ import annotations

from collections.abc import Callable

from lscs.advisor.names import armor_zh, choice_option_text, choice_title, minion_label
from lscs.state.models import Event, EventType, GameState, Minion, TribesConfidence

Namer = Callable[[str], str]
KEY_EVENTS = (
    EventType.GAME_STARTED,
    EventType.HEROES_OFFERED,
    EventType.HERO_CHOSEN,
    EventType.RECRUIT_STARTED,
    EventType.COMBAT_STARTED,
    EventType.COMBAT_ENDED,
    EventType.GAME_ENDED,
    EventType.CHOICE_OFFERED,
)


def _identity(card_id: str) -> str:
    return card_id


def _minion(m: Minion, name: Namer) -> str:
    return f"{minion_label(m, name)} {m.atk}/{m.health}"


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
            case EventType.HEROES_OFFERED if state.hero_select is not None:
                heroes = "、".join(
                    f"{name(h.card_id)}（{armor_zh(h.armor)}）" for h in state.hero_select.offered
                )
                again = "重新随机：" if event.data.get("changed") else ""
                lines.append(f"{prefix} {again}备选英雄 {heroes}")
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
            case EventType.CHOICE_OFFERED if state.choice is not None:
                choice = state.choice
                options = "、".join(choice_option_text(o, name) for o in choice.options)
                count = f"{len(choice.options)} 选 1"
                lines.append(f"{prefix} 选择 {choice_title(choice, name)}（{count}）：{options}")
            case EventType.CHOICE_RESOLVED:
                card = event.data.get("card")
                lines.append(f"{prefix} 已选 {name(str(card))}" if card else f"{prefix} 选择结束")
            case _:
                lines.append(f"{prefix} {event.type.value}")
    if state.uncertain and lines:
        lines.append("      ⚠ 日志解析出错过，状态可能不准")
    return lines
