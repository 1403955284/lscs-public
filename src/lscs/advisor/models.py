"""建议的数据模型（不可变）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from lscs.advisor.pool import PoolNote

ActionType = Literal["buy", "sell", "upgrade", "hero_power", "refresh", "freeze"]
# AI 建议的四路：快速建议、深度分析、选英雄、选项（游戏弹出的"选一个"）；
# review 是对局复盘（每局一次，只出现在调用记录里，不走调度器）
Lane = Literal["fast", "deep", "hero", "choice", "review"]
# 深度分析的操作为什么不能用了：refreshed 商店刷新了；invalid 操作不合法；
# changed 步骤已经做完、局面变化太大或已经进入战斗
Expired = Literal["refreshed", "invalid", "changed"]
ACTION_TYPES: tuple[str, ...] = ("buy", "sell", "upgrade", "hero_power", "refresh", "freeze")
# 站位建议里一个随从为什么要挪（站位建议设计文档第 3 节）：战斗型亡语、进击放最左，复仇放最右，
# 对面有劈砍时唯一的嘲讽靠边
PositionRole = Literal["deathrattle", "rally", "avenge", "taunt_edge"]
# 整局计划的阶段（整局计划与流派指南设计文档第 4.1 节）
Stage = Literal["前期", "过渡", "成型", "保血"]
STAGES: tuple[str, ...] = ("前期", "过渡", "成型", "保血")
UNDECIDED = "未定"  # 前几回合还没定目标流派
FIT_SUFFIX = "更好"  # StatRow.fit 的结尾："配野兽更好"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Action(_Frozen):
    type: ActionType
    entity_id: int | None = None  # buy / sell 指向的实体；hero_power 需要目标时是目标
    reason: str = ""


class GamePlan(_Frozen):
    """整局计划（深度分析定下并维护；快速建议和面板跟着它走）。每个字段都短，面板放得下。"""

    comp: str  # 目标流派：指南里的名字或 AI 自己起的短名；还没定时 "未定"
    tribe: str | None = None  # 主种族（Race 枚举名，如 "ABERRATION"）；没有或不在本局时 None
    key_cards: tuple[str, ...] = ()  # 2~3 张核心卡的卡名（调度器校验过：本局卡池里有）
    tiers: str = ""  # 升本节奏，一句话："第 6 回合 4 本，第 8 回合 5 本"
    roll: str = ""  # 刷新 / 保血的规则，一句话
    stage: Stage | None = None  # 现在的阶段
    pivot: str = ""  # 换方向的条件，一句话
    changed: str = ""  # 这次改计划的原因（没改为空）
    turn: int = 0  # 定下 / 最近一次修改的回合

    def signature(self) -> tuple:
        """看计划本身有没有变：目标流派（调度器已经对到指南标题）和核心卡（不看顺序）。阶段另算；
        主种族、升本节奏、刷新规则、换方向条件只是措辞，AI 每回合换个说法不算改计划。"""
        return (self.comp, frozenset(self.key_cards))

    @property
    def undecided(self) -> bool:
        return self.comp == UNDECIDED


class Plan(_Frozen):
    source: Literal["rules", "llm"]
    actions: tuple[Action, ...] = ()
    summary: str = ""
    direction: str | None = None
    confidence: float | None = None
    analysis: str = ""  # AI 的局势判断、本局方向、本回合目标
    look_for: tuple[str, ...] = ()  # AI 说接下来要找的牌
    # AI 回答里的整局计划：解析时是原样（卡名没校验），调度器交付前校验过、填好回合
    game_plan: GamePlan | None = None

    def signature(self) -> tuple[tuple[str, int | None], ...]:
        """只看动作和对象、不看理由，用于判断两个方案是否相同。"""
        return tuple((a.type, a.entity_id) for a in self.actions)


class Advice(_Frozen):
    state_seq: int
    turn: int
    plan: Plan
    alternatives: tuple[Plan, ...] = ()
    stale_note: str | None = None
    elapsed: float | None = None  # AI 调用耗时（秒）
    lane: Lane = "fast"
    expired: Expired | None = None  # 深度分析的操作已经不能用，只看思路
    # 对子和要找的牌在池里还剩约几张（advisor.pool.pool_notes）；没有卡牌数据时为空
    pool_notes: tuple[PoolNote, ...] = ()
    # 方案和整局计划明显矛盾时的一句提醒（advisor.plan_check.off_plan_note）；只提醒不改方案
    plan_note: str | None = None


class HeroComment(_Frozen):
    entity_id: int
    text: str  # 一句点评，最多 20 字


class HeroAdvice(_Frozen):
    """选英雄建议（规则或 AI）。"""

    source: Literal["rules", "llm"]
    signature: tuple[tuple[int, str], ...]  # 基于哪一组备选（HeroSelect.signature()）
    pick: int | None  # 推荐的实体编号；规则拿不准时为 None
    reason: str
    comments: tuple[HeroComment, ...] = ()
    elapsed: float | None = None  # AI 调用耗时（秒）
    confidence: float | None = None  # AI 的把握，只记录不显示
    # 规则按什么推荐：stats 本版本的平均名次（四个都有可靠的统计时），armor 护甲
    basis: Literal["stats", "armor"] = "armor"


class StatRow(_Frozen):
    """本版本统计里的一个备选英雄或饰品（本版本统计设计文档第 6.1 节）。"""

    entity_id: int
    text: str  # 短句："第 3.67 名 · 选 28% · 第 5/116"（样本少："第 4.20 名（样本少）"）
    score: float  # 排序用：平均名次，越小越好
    low: bool = False  # 样本少：照样显示，不参与"战绩最好"
    # 选英雄配种族（流派指南自动更新设计文档第 6.2 节）："配野兽更好"、"配野兽、亡灵更好"；
    # 没有时为空
    fit: str = ""

    @property
    def fit_short(self) -> str:
        """面板精简模式用的短写："配野兽"。"""
        return self.fit.removesuffix(FIT_SUFFIX)


class StatsNote(_Frozen):
    """一组备选英雄或饰品选项的本版本统计（终端的 📊 块、面板的条目）。"""

    kind: Literal["hero", "trinket"]
    signature: tuple[tuple[int, str], ...] = ()  # hero：基于哪一组备选
    choice_id: int | None = None  # trinket：基于哪一组选项
    rows: tuple[StatRow, ...]  # 按备选 / 选项的画面顺序；没有数据的不在里面
    best: int | None  # 战绩最好的实体（样本少的不参与；并列取靠前）
    source: str  # MetaStats.source_text()


class ChoiceAdvice(_Frozen):
    """AI 在游戏弹出的"选一个"里推荐的选项（没有规则版本）。"""

    choice_id: int  # 基于哪一组（Choice.id）
    pick: int  # 推荐的实体编号
    reason: str
    elapsed: float | None = None  # AI 调用耗时（秒）
    confidence: float | None = None  # AI 的把握，只记录不显示


class Move(_Frozen):
    """站位建议的一步：把一个随从拖到某个槽位。"""

    entity_id: int
    index: int  # 目标槽位，0 起（按做完前面几步之后的顺序）
    before_id: int | None  # 拖到谁的左边（拖完后紧挨在它右边的随从）；最左边 / 最右边时为 None
    role: PositionRole
    reason: str  # 终端的理由
    short: str  # 面板的短语


class PositionAdvice(_Frozen):
    """站位建议（规则，没有 AI 版本）。"""

    state_seq: int
    turn: int
    board: tuple[int, ...]  # 计算时我方场上的实体顺序（从左到右）
    target: tuple[int, ...]  # 目标顺序：board 的一个排列
    moves: tuple[Move, ...]  # 最多 MAX_MOVES 步；为空表示现在的顺序没问题
    more: int = 0  # 没显示的剩余步数
    cleave: str | None = None  # 下一个对手上次阵容里的劈砍随从（卡牌 ID）
    intro: bool = False  # 本局第一次有站位建议：终端多打一行机制说明
