"""站位建议（站位建议设计文档第 3 节）：买完牌后把哪个随从拖到哪，纯规则、瞬间算完。

只讲战斗机制能推出来的几步：战斗型亡语、进击放最左（随从从左到右依次出手），复仇放最右（要等
友方先倒下），下一个对手上次阵容里有劈砍、我方恰好一个嘲讽时把它放最左（只有一个邻居）。
其他随从（白板、圣盾、剧毒、风怒……）从不被点名，只是被挤开。亡语、进击、复仇、劈砍都要看
卡牌说明：没有卡牌数据时不提示（由调用方决定）。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from lscs.advisor.models import Move, PositionAdvice, PositionRole
from lscs.cards.db import CardDB, CardInfo
from lscs.state.models import GameState, Minion

Namer = Callable[[str], str]
# 为什么一个带亡语、进击的随从不挪：说明里提到本随从左边 / 右边的随从（拖到边上就落空）；
# 战斗开始时立即发起攻击（英勇的巢母）；亡语只是拿牌、改酒馆
Exclusion = Literal["adjacency", "attacks_at_start", "economy_deathrattle"]
EXCLUSIONS: tuple[Exclusion, ...] = ("adjacency", "attacks_at_start", "economy_deathrattle")
MAX_MOVES = 2  # 一回合最多显示几步；做完后重算，剩下的会再出现
LEFT_ROLES: tuple[PositionRole, ...] = ("deathrattle", "rally")  # 放最左，亡语在前

_AVENGE = re.compile(r"复仇[（(]\d")
_DEATHRATTLE = re.compile(r"亡语[：:]")
_RALLY = re.compile(r"进击[：:]")
# 亡语只是拿牌、改酒馆：早死晚死结果一样，不为它调站位。只看亡语那一句（到第一个"。"或"（"
# 为止）：无面转化者括号里的"每施放一个酒馆法术都会提升"不算
_ECONOMY = re.compile(r"获取|酒馆|鲜血宝石|刷新")
_SENTENCE_END = re.compile(r"[。（]")
# 说明里提到本随从左边 / 右边的随从（缝合回收者"消灭本随从左边的随从"、海床招募者"本随从右边的
# 随从"）：拖到最边上就没有那个邻居了。"最左边的随从"说的是场上的位置，不算
_ADJACENT = re.compile(r"(?<!最)[左右]边")
# 劈砍：攻击时连带打攻击目标相邻的随从（野火元素"对一个相邻的敌人"是击杀后的溢出伤害，不算）
_CLEAVE = re.compile(r"(攻击目标|目标及)[^。]{0,6}相邻")
_ATTACKS_AT_START = "战斗开始时：立即发起攻击"  # 英勇的巢母：进击和位置无关

# 终端的理由、面板的短语（第 3.2 节）
REASONS: dict[PositionRole, tuple[str, str]] = {
    "deathrattle": ("亡语随从先出手、先倒下，亡语最早生效，后面的随从都能用上", "亡语最早触发"),
    "rally": ("进击只在它攻击时触发，放左边先出手，别还没打就被打死", "进击要先出手"),
    "avenge": ("复仇要等友方先倒下几个才触发，放右边活到那时候", "复仇放右边等触发"),
    "taunt_edge": (
        "对面有 {cleave}（打一个连带打旁边的），嘲讽会被它打：放到最边上只连累一个邻居",
        "对面有劈砍，嘲讽靠边",
    ),
}
INTRO = (
    "站位规则：战斗时随从从最左边开始依次出手；亡语召唤的随从出现在原位；嘲讽会被优先攻击。"
    "本局只提示这一次"
)


def _classify(info: CardInfo) -> PositionRole | Exclusion | None:
    """按顺序判断，先命中的算（站位建议设计文档第 3.1 节；邻居那条在最前面：放最左、最右都会
    落空）。"""
    mechanics, text = info.mechanics, info.text
    if _ADJACENT.search(text):
        return "adjacency"
    if "AVENGE" in mechanics or _AVENGE.search(text):
        return "avenge"
    if _ATTACKS_AT_START in text:
        return "attacks_at_start"
    marker = _DEATHRATTLE.search(text)
    if "DEATHRATTLE" in mechanics or marker:
        effect = text[marker.end() :] if marker else text
        sentence = _SENTENCE_END.split(effect, maxsplit=1)[0]
        return "economy_deathrattle" if _ECONOMY.search(sentence) else "deathrattle"
    if "BACON_RALLY" in mechanics or _RALLY.search(text):
        return "rally"
    return None


def combat_role(m: Minion, info: CardInfo | None) -> PositionRole | None:
    """这个随从在战斗里要不要挪、往哪挪（只看卡牌数据；嘲讽靠边由 plan_positions 定）。"""
    found = _classify(info) if info is not None else None
    if found is None or found in EXCLUSIONS:
        return None
    return cast(PositionRole, found)


def exclusion(info: CardInfo | None) -> Exclusion | None:
    """带亡语、进击的随从为什么不挪（哪条规则排除了它）；有角色或者本来就没什么可挪的时为 None。"""
    found = _classify(info) if info is not None else None
    return cast(Exclusion, found) if found in EXCLUSIONS else None


def is_cleave(info: CardInfo | None) -> bool:
    """攻击时连带打攻击目标相邻的随从（刀剑收藏家、黑曜石掠夺者）。"""
    return info is not None and bool(_CLEAVE.search(info.text))


def cleave_threat(state: GameState, cards: CardDB) -> str | None:
    """下一个对手（残影也算）上次阵容里的劈砍随从的卡牌 ID；没有下一个对手、没见过阵容、
    没有劈砍时为 None。"""
    if state.next_opponent_id is None:
        return None
    opp = state.opponents.get(state.next_opponent_id)
    if opp is None:
        return None
    return next((m.card_id for m in opp.last_board if is_cleave(cards.get(m.card_id))), None)


def apply_move(order: Sequence[int], entity_id: int, index: int) -> tuple[int, ...]:
    """把 entity_id 拖到第 index 个槽位（0 起），中间的随从各挪一格。"""
    rest = [e for e in order if e != entity_id]
    rest.insert(index, entity_id)
    return tuple(rest)


def _neighbour(order: Sequence[int], index: int) -> int | None:
    """拖到 index 后要写"拖到谁的左边"：最左边、最右边写位置，不写随从。"""
    return order[index + 1] if 0 < index < len(order) - 1 else None


def moves_toward(
    board: Sequence[int], target: Sequence[int], left: int | None = None
) -> list[tuple[int, int, int | None]]:
    """(要拖的实体, 目标槽位, 拖到谁的左边) 列表。前 left 个槽位（None 表示全部）从左到右逐个
    看：这个槽位不对就把该在这里的随从拖过来（它右边的全部右移一格）；剩下的槽位从右到左逐个
    看，把该在这里的随从往右拖。中间的随从相对顺序不变，所以从不被点名。"""
    order = tuple(board)
    count = len(order)
    left = count if left is None else left
    steps: list[tuple[int, int, int | None]] = []
    for index in [*range(left), *range(count - 1, left - 1, -1)]:
        entity = target[index]
        if order[index] == entity:
            continue
        order = apply_move(order, entity, index)
        steps.append((entity, index, _neighbour(order, index)))
    return steps


def plan_positions(state: GameState, cards: CardDB, name: Namer) -> PositionAdvice | None:
    """我方场上少于 2 个随从时为 None；moves 为空表示现在的顺序没问题。"""
    me = state.me
    if me is None or len(me.board) < 2:
        return None
    board = me.board
    roles = {m.entity_id: combat_role(m, cards.get(m.card_id)) for m in board}
    place = {m.entity_id: i for i, m in enumerate(board)}
    cleave = cleave_threat(state, cards)
    taunts = [m.entity_id for m in board if "TAUNT" in m.keywords]
    # 2 个以上嘲讽一步解决不了，不提示（站位建议设计文档第 2.4 节）
    edge = taunts if cleave is not None and len(taunts) == 1 else []
    for entity in edge:
        roles[entity] = "taunt_edge"
    avenges = [m.entity_id for m in board if roles[m.entity_id] == "avenge"]
    # 已经在最右边的嘲讽也只有一个邻居：不用动，除非复仇随从要那个槽位
    edge_right = bool(edge) and place[edge[0]] == len(board) - 1 and not avenges
    left = sorted(
        (e for e, role in roles.items() if role in LEFT_ROLES),
        key=lambda e: (LEFT_ROLES.index(roles[e]), place[e]),  # type: ignore[arg-type]
    )
    right = [*avenges, *edge] if edge_right else avenges
    head = [] if edge_right else edge
    fixed = {*edge, *left, *right}
    middle = [m.entity_id for m in board if m.entity_id not in fixed]
    target = (*head, *left, *middle, *right)
    ids = tuple(m.entity_id for m in board)
    steps = moves_toward(ids, target, left=len(head) + len(left))
    moves = []
    for entity, index, before in steps[:MAX_MOVES]:
        role = roles[entity]
        assert role is not None  # 只有带角色的随从会被拖
        reason, short = REASONS[role]
        if role == "taunt_edge":
            reason = reason.format(cleave=name(cleave or ""))
        moves.append(
            Move(
                entity_id=entity,
                index=index,
                before_id=before,
                role=role,
                reason=reason,
                short=short,
            )
        )
    return PositionAdvice(
        state_seq=state.seq,
        turn=state.turn,
        board=ids,
        target=target,
        moves=tuple(moves),
        more=max(0, len(steps) - MAX_MOVES),
        cleave=cleave,
    )


def validate_position(advice: PositionAdvice, board: Sequence[int]) -> str | None:
    """合法返回 None；否则返回第一条不满足的原因（中文）。board 是现在我方场上的实体顺序。"""
    start = tuple(advice.board)
    if start != tuple(board):
        return "场面变了"
    if len(set(advice.target)) != len(advice.target) or sorted(advice.target) != sorted(start):
        return "目标顺序不是场上随从的排列"
    if len(advice.moves) > MAX_MOVES:
        return f"步数超过 {MAX_MOVES} 步"
    order = start
    reach, keep_from = 0, len(order)  # 往左拖固定的前缀、往右拖固定的后缀
    for step, move in enumerate(advice.moves, start=1):
        where = f"第 {step} 步"
        if move.entity_id not in order:
            return f"{where}：场上没有 #{move.entity_id}"
        if not 0 <= move.index < len(order):
            return f"{where}：没有第 {move.index + 1} 个槽位"
        if move.before_id is not None and move.before_id not in order:
            return f"{where}：场上没有 #{move.before_id}"
        current = order.index(move.entity_id)
        if current == move.index:
            return f"{where}：#{move.entity_id} 已经在第 {move.index + 1} 个，原地不动"
        order = apply_move(order, move.entity_id, move.index)
        if move.before_id != _neighbour(order, move.index):
            return f"{where}：拖完后右边不是 #{move.before_id}"
        if move.index < current:
            reach = max(reach, move.index + 1)
        else:
            keep_from = min(keep_from, move.index)
    target = tuple(advice.target)
    if order[:reach] != target[:reach] or order[keep_from:] != target[keep_from:]:
        return "照着做完后和目标顺序不一致"
    return None


@dataclass(frozen=True)
class StepText:
    """一步的写法：把 左数第 number 个 card 拖到……"""

    move: Move
    number: int  # 拖之前它是左数第几个（1 起）
    card_id: str
    where: str  # "拖到最左边" / "拖到最右边" / "拖到 ×× 的左边"


def where_text(move: Move, card_of: Mapping[int, str], name: Namer) -> str:
    if move.before_id is None:
        return "拖到最左边" if move.index == 0 else "拖到最右边"
    card = card_of.get(move.before_id)
    return f"拖到 {name(card) if card else f'#{move.before_id}'} 的左边"


def step_texts(advice: PositionAdvice, card_of: Mapping[int, str], name: Namer) -> list[StepText]:
    """按顺序把每一步套在 advice.board 上：第二步的"左数第几个"是做完第一步之后的位置。
    card_of：实体编号 → 卡牌 ID（我方场上的随从）。"""
    order = tuple(advice.board)
    texts = []
    for move in advice.moves:
        number = order.index(move.entity_id) + 1 if move.entity_id in order else 0
        card = card_of.get(move.entity_id, f"#{move.entity_id}")
        texts.append(StepText(move, number, card, where_text(move, card_of, name)))
        order = apply_move(order, move.entity_id, move.index)
    return texts
