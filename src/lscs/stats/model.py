"""本版本统计的数据模型与解析（本版本统计设计文档第 5.1~5.3 节）。

只做相对比较：Firestone 的用户偏强，几乎所有英雄的平均名次都在 4.5 以下，所以英雄按平均名次
在样本够的英雄里排名，饰品只在同一组选项里比。随从按"打出当回合"比较（回合修正），避开
"活到后期的人本来名次就好"的幸存者偏差。样本少的条目标出来、不参与推荐。

英雄的 tribeStats 读成"配某族更好"（流派指南自动更新设计文档第 6.1 节）：本局有这个种族时比没有时
平均名次好 0.15 以上（impactAveragePositionVsMissingTribe ≤ TRIBE_FIT）、两边样本都够的种族，
从好到差最多 MAX_FIT_TRIBES 个。选英雄时本局种族还不知道，只当小小的加分。
"""

from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from typing import Any

from hearthstone.enums import Race
from pydantic import BaseModel, ConfigDict

from lscs.advisor.names import TRIBE_ZH
from lscs.config import StatsConfig

log = logging.getLogger(__name__)

MIN_HERO_POINTS = 1000  # 全部英雄现在最少 1863；补丁刚更新那两天会低于它
MIN_TRINKET_POINTS = 500  # 241 个饰品里 7 个低于它
MIN_CARD_PLAYS = 2000  # 有数据的卡池随从全部 ≥ 5000，只为防补丁初期
MIN_TURN_PLAYS = 200  # 回合修正只算打出 ≥ 200 次的回合
MIN_TRIBE_POINTS = 1000  # 有这个种族、没有这个种族的局数都要够
TRIBE_FIT = -0.15  # 有 vs 没有的平均名次差（负数是更好）
MAX_FIT_TRIBES = 2
PERIOD_ZH = {"last-patch": "本补丁", "past-seven": "最近 7 天"}
MMR_ZH = {100: "全部玩家", 50: "前 50%", 25: "前 25%", 10: "前 10%", 1: "前 1%"}
PANEL_SOURCE = "Firestone"
_SKIN = re.compile(r"_SKIN_.*$")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class TribeFit(_Frozen):
    """本局有这个种族时，这个英雄的平均名次比没有时好多少。"""

    race: str  # Race 枚举名
    impact: float  # impactAveragePositionVsMissingTribe，保留两位（负数是更好）
    with_points: int  # dataPoints：本局有这个种族的局数
    without_points: int  # dataPointsOnMissingTribe：没有的局数


class HeroStat(_Frozen):
    card_id: str
    data_points: int
    average_position: float  # 保留两位
    pick_rate: float  # totalPicked / totalOffered，0~1
    rank: int | None  # 在样本够的英雄里按平均名次排第几（1 起）；样本少时为 None
    low: bool  # data_points < MIN_HERO_POINTS
    fits: tuple[TribeFit, ...] = ()  # 配哪个种族更好：按 impact 从好到差，最多 MAX_FIT_TRIBES 个


class TrinketStat(_Frozen):
    card_id: str
    data_points: int  # 所选分段的 dataPoints
    average_placement: float  # 保留两位（同一组里比较、并列取靠前的）
    pick_rate: float
    low: bool  # data_points < MIN_TRINKET_POINTS


class CardStat(_Frozen):
    card_id: str
    played: int  # totalPlayed
    impact: float  # 回合修正后的"名次提升"，保留两位
    low: bool  # played < MIN_CARD_PLAYS


class MetaStats(_Frozen):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    updated: datetime  # 三个文件里最早的 lastUpdateDate（UTC）
    period: str  # "last-patch" / "past-seven"
    mmr: int  # 100 / 50 / 25 / 10 / 1
    games: int  # 英雄文件的 dataPoints（显示用）
    hero_total: int  # 排名的分母：样本够的英雄个数
    heroes: dict[str, HeroStat]
    trinkets: dict[str, TrinketStat]
    cards: dict[str, CardStat]  # [stats] cards = false 时为空
    hero_median: float | None = None  # 样本够的英雄的平均名次中位数（提示词里的基准）
    cached_age: float | None = None  # 用的是旧缓存时，缓存多少秒前写的；新下载的为 None
    tz: tzinfo | None = None  # 显示时间用的时区；None 表示本机的时区

    def hero(self, card_id: str) -> HeroStat | None:
        """先精确，再去掉皮肤后缀（_SKIN_A 之类）。"""
        found = self.heroes.get(card_id)
        if found is None:
            found = self.heroes.get(_SKIN.sub("", card_id))
        return found

    def trinket(self, card_id: str) -> TrinketStat | None:
        """先精确，再去掉末尾的 t（带 t 和不带的是两条，日志里出现的通常是带 t 的）。"""
        found = self.trinkets.get(card_id)
        if found is None and card_id.endswith("t"):
            found = self.trinkets.get(card_id[:-1])
        return found

    def card(self, card_id: str) -> CardStat | None:
        """调用方先用 CardDB.normal_id 换成普通卡。"""
        return self.cards.get(card_id)

    @property
    def period_text(self) -> str:
        return PERIOD_ZH.get(self.period, self.period)

    @property
    def mmr_text(self) -> str:
        return MMR_ZH.get(self.mmr, f"前 {self.mmr}%")

    @property
    def local_updated(self) -> datetime:
        """更新时间换成显示用的时区（玩家那边的本地时间）。"""
        return self.updated.astimezone(self.tz)

    @property
    def date_text(self) -> str:
        return f"{self.local_updated:%m-%d}"

    def source_text(self) -> str:
        """终端的署名："数据来自 Firestone · 本补丁 · 全部玩家 · 10-14 更新"。"""
        return f"数据来自 Firestone · {self.period_text} · {self.mmr_text} · {self.date_text} 更新"

    def panel_source(self) -> str:
        return PANEL_SOURCE


def _num(entry: dict, key: str) -> float:
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{key} 不是数字")
    return value


def _read(path: Path, key: str) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        raise ValueError(f"{path.name} 里没有 {key}")
    return data


def _date(data: dict) -> datetime | None:
    text = data.get("lastUpdateDate")
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _entries(data: dict, key: str, what: str, parse: Any) -> list:
    """逐个解析条目；缺字段、类型不对的跳过并记调试日志，不让整个文件作废。"""
    parsed = []
    for raw in data[key]:
        try:
            item = parse(raw)
        except (KeyError, TypeError, ValueError, AttributeError):
            log.debug("跳过一条有误的%s统计：%r", what, raw, exc_info=True)
            continue
        if item is not None:
            parsed.append(item)
    return parsed


def _fit(raw: dict) -> TribeFit | None:
    """一条 tribeStats：不认识的种族、样本不够、差得不够多时为 None；缺字段、类型不对时抛出。"""
    tribe = raw["tribe"]
    if isinstance(tribe, bool) or not isinstance(tribe, int):
        raise TypeError("tribe 不是整数")
    try:
        race = Race(tribe).name
    except ValueError:
        return None
    if race not in TRIBE_ZH or race == "ALL":
        return None
    with_points = int(_num(raw, "dataPoints"))
    without_points = int(_num(raw, "dataPointsOnMissingTribe"))
    impact = _num(raw, "impactAveragePositionVsMissingTribe")
    if min(with_points, without_points) < MIN_TRIBE_POINTS or impact > TRIBE_FIT:
        return None
    return TribeFit(
        race=race,
        impact=round(impact, 2),
        with_points=with_points,
        without_points=without_points,
    )


def _fits(raw: dict) -> tuple[TribeFit, ...]:
    """英雄的 tribeStats → 配哪个种族更好；没有、不是列表时为空，有误的条目跳过。"""
    tribes = raw.get("tribeStats")
    if not isinstance(tribes, list):
        return ()
    fits = []
    for entry in tribes:
        try:
            fit = _fit(entry) if isinstance(entry, dict) else None
        except (KeyError, TypeError, ValueError):
            log.debug("跳过一条有误的种族统计：%r", entry, exc_info=True)
            continue
        if fit is not None:
            fits.append(fit)
    fits.sort(key=lambda f: (f.impact, f.race))
    return tuple(fits[:MAX_FIT_TRIBES])


def _hero(raw: dict) -> HeroStat:
    card_id = raw["heroCardId"]
    if not isinstance(card_id, str):
        raise TypeError("heroCardId 不是字符串")
    points = int(_num(raw, "dataPoints"))
    offered, picked = _num(raw, "totalOffered"), _num(raw, "totalPicked")
    return HeroStat(
        card_id=card_id,
        data_points=points,
        average_position=round(_num(raw, "averagePosition"), 2),
        pick_rate=picked / offered if offered else 0.0,
        rank=None,
        low=points < MIN_HERO_POINTS,
        fits=_fits(raw),
    )


def _at_mmr(raw: dict, key: str, mmr: int) -> dict | None:
    for entry in raw.get(key) or ():
        if isinstance(entry, dict) and entry.get("mmr") == mmr:
            return entry
    return None


def _trinket(raw: dict, mmr: int) -> TrinketStat:
    card_id = raw["trinketCardId"]
    if not isinstance(card_id, str):
        raise TypeError("trinketCardId 不是字符串")
    placement, pick = _at_mmr(raw, "averagePlacementAtMmr", mmr), _at_mmr(raw, "pickRateAtMmr", mmr)
    if placement is not None and pick is not None:
        points = int(_num(placement, "dataPoints"))
        average, rate = _num(placement, "placement"), _num(pick, "pickRate")
    else:  # 条目里没有这个分段：退回全部玩家
        log.debug("饰品 %s 没有%s的数据，用全部玩家的", card_id, MMR_ZH.get(mmr, mmr))
        points = int(_num(raw, "dataPoints"))
        average, rate = _num(raw, "averagePlacement"), _num(raw, "pickRate")
    return TrinketStat(
        card_id=card_id,
        data_points=points,
        average_placement=round(average, 2),
        pick_rate=rate,
        low=points < MIN_TRINKET_POINTS,
    )


def _card(raw: dict) -> CardStat | None:
    """回合修正：Σ n_t·(other_t − played_t) / Σ n_t，只算打出 ≥ MIN_TURN_PLAYS 次、两个平均值
    都有的回合；一个回合都不够时不收录（None）。"""
    card_id = raw["cardId"]
    if not isinstance(card_id, str):
        raise TypeError("cardId 不是字符串")
    played = int(_num(raw, "totalPlayed"))
    total = weighted = 0.0
    for turn in raw["turnStats"]:
        if not isinstance(turn, dict):
            continue
        n, mine, other = (
            turn.get("totalPlayed"),
            turn.get("averagePlacement"),
            turn.get("averagePlacementOther"),
        )
        numbers = (n, mine, other)
        if any(isinstance(v, bool) or not isinstance(v, int | float) for v in numbers):
            continue  # 个别回合的 averagePlacementOther 是 null
        if n < MIN_TURN_PLAYS:
            continue
        total += n
        weighted += n * (other - mine)
    if total == 0:
        return None
    return CardStat(
        card_id=card_id,
        played=played,
        impact=round(weighted / total, 2),
        low=played < MIN_CARD_PLAYS,
    )


def _ranked(heroes: list[HeroStat]) -> dict[str, HeroStat]:
    """样本够的英雄按平均名次升序、再按卡牌 ID 排名（1 起）。"""
    order = sorted((h for h in heroes if not h.low), key=lambda h: (h.average_position, h.card_id))
    ranks = {h.card_id: i for i, h in enumerate(order, start=1)}
    return {h.card_id: h.model_copy(update={"rank": ranks.get(h.card_id)}) for h in heroes}


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def load_meta_stats(
    hero_path: Path,
    trinket_path: Path,
    card_path: Path | None,
    cfg: StatsConfig,
    tz: tzinfo | None = None,
) -> MetaStats:
    """读三个（[stats] cards = false 时两个）缓存文件。顶层键缺失时抛 ValueError。
    tz：显示时间用的时区（None 表示本机的时区）；更新时间本身照旧存成 UTC。"""
    hero_data = _read(hero_path, "heroStats")
    trinket_data = _read(trinket_path, "trinketStats")
    card_data = _read(card_path, "cardStats") if card_path is not None else None
    dates = [_date(d) for d in (hero_data, trinket_data, card_data) if d is not None]
    known = [d for d in dates if d is not None]
    if not known:
        raise ValueError("统计数据里没有更新时间")
    heroes = _ranked(_entries(hero_data, "heroStats", "英雄", _hero))
    mmr = cfg.mmr_percentile
    trinkets = _entries(trinket_data, "trinketStats", "饰品", lambda raw: _trinket(raw, mmr))
    cards = _entries(card_data, "cardStats", "随从", _card) if card_data is not None else []
    games = hero_data.get("dataPoints")
    return MetaStats(
        updated=min(known),
        period=cfg.period,
        mmr=mmr,
        games=games if isinstance(games, int) and not isinstance(games, bool) else 0,
        hero_total=sum(1 for h in heroes.values() if not h.low),
        heroes=heroes,
        trinkets={t.card_id: t for t in trinkets},
        cards={c.card_id: c for c in cards},
        hero_median=_median([h.average_position for h in heroes.values() if not h.low]),
        tz=tz,
    )
