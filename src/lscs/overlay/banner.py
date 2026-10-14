"""棋盘顶部的战斗结果横幅（提前显示战斗结果设计文档第 4、5.3 节）：什么时候显示、写什么。

面板的数据模型（overlay.model）只把它放进快照（OverlayView.banner），画面高亮窗口
（overlay.highlight）把它画在棋盘顶部，面板自己不画。纯逻辑，不碰 Tk。

结果结算了就显示，一直留到下一回合招募真正开始：日志里战斗一写完（不到 2 秒）就换了回合，阶段
已经是下一回合的招募，但 STEP 还没到 MAIN_ACTION，画面上的战斗动画还要播 20~70 秒，所以看
RECRUIT_STARTED 和 main_action，不看阶段。对局结束那场从结果到达算起留 20 秒（窗口按 expires_at
自己清掉，免得一直盖在大厅上）。招募开始以后、选英雄、没有对局时没有。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from lscs.advisor.names import combat_banner_text
from lscs.state.models import Event, EventType, GameState, Phase

Namer = Callable[[str], str]
BANNER_LINGER_SECONDS = 20  # 对局结束那场的横幅留多久（常量，不进配置）


@dataclass(frozen=True)
class Banner:
    """画在画面高亮窗口上的一行字。"""

    text: str  # "本场已定：赢 · 洛卡拉掉 5 血"
    tone: Literal["win", "loss", "tie"]  # 决定颜色
    expires_at: float | None = None  # 对局结束那场：monotonic 时刻，过了就不画


class BannerTracker:
    """记下最近一次结果到达的时刻和最近一次招募真正开始的回合，按当前快照算出横幅。
    不加锁：由调用方（面板的数据模型，在它自己的锁里）调用。"""

    def __init__(self, enabled: bool = True, clock: Callable[[], float] | None = None) -> None:
        """enabled：config.toml 的 [combat_result]；关掉时永远没有横幅。"""
        self._enabled = enabled
        self._clock = clock or time.monotonic
        self._result_at: float | None = None  # 最近一次收到战斗结果的时刻
        self._recruited: int | None = None  # 最近一次招募真正开始（RECRUIT_STARTED）的回合

    def on_update(self, events: list[Event]) -> None:
        for event in events:
            if event.type == EventType.GAME_STARTED:
                self._recruited = None
            elif event.type == EventType.COMBAT_RESULT:
                self._result_at = self._clock()
            elif event.type == EventType.RECRUIT_STARTED:
                self._recruited = event.turn

    def current(self, state: GameState | None, name: Namer) -> Banner | None:
        """现在该画的横幅；没有时 None。"""
        if not self._enabled or state is None or state.last_combat is None:
            return None
        result = state.last_combat
        expires_at = None
        if state.phase == Phase.COMBAT:
            showing = result.turn == state.turn
        elif state.phase == Phase.RECRUIT:
            showing = (
                state.turn == result.turn + 1
                and not state.main_action
                and self._recruited != state.turn
            )
        elif state.phase == Phase.GAME_OVER and self._result_at is not None:
            expires_at = self._result_at + BANNER_LINGER_SECONDS
            showing = result.turn == state.turn and self._clock() < expires_at
        else:
            showing = False
        if not showing:
            return None
        return Banner(combat_banner_text(result, name), result.outcome, expires_at)
