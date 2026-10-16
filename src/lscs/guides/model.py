"""HSReplay 流派指南的模型、翻译和转成流派指南的段（流派指南自动更新设计文档第 3.2~3.4 节）。

只用当前补丁更新过的条目（last_updated 的 UTC 日期 ≥ [guides] patch_date，且不隐藏）。卡名引用
[[English Name||dbfId]] 换成卡牌数据里的中文名，正文仍是英文原文；核心卡、配合卡只留 1~6 本的卡池
随从。每条的显示名是"种族中文·代表卡中文名"（≤ PLAN_COMP_CHARS 字），既是发给 AI 的段标题，
也是面板"计划"行里的流派名。

to_comps_file 把它们写成 llm.comps 的 CompsFile：代替 comps.md 的流派段（第 3.5 节），comps.md 的
通用运营段接在最后；select_comps、comps_block、match_comp 不用改规则。
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path

from hearthstone.enums import Race

from lscs.advisor.names import TRIBE_ZH, tribe_zh
from lscs.cards.db import CardDB
from lscs.llm.comps import ALL, CompGuide, CompsFile
from lscs.llm.parse import PLAN_COMP_CHARS
from lscs.llm.prompt import MAX_TAVERN_TIER

log = logging.getLogger(__name__)

SOURCE = "HSReplay"
# HSReplay 的指南块最多几个字（约 3800 token；第 4 节）。9000 时 5 个种族的 462 种大厅里有 143 种
# 放不下最后一两段（复审实测），加到 10500
LIVE_MAX_CHARS = 10500
DIFFICULTY_ZH = {1: "低", 2: "中", 3: "高"}
DEFAULT_TIER = 3  # 梯队不在 1~3 时按最弱
DEFAULT_DIFFICULTY = 2  # 难度不在 1~3 时按中等
NEUTRAL = "中立"
SEASON = "本赛季"
_REF = re.compile(r"\[\[([^\]|]+)\|\|(\d+)\]\]")
_TEXTS = ("summary", "when_to_commit", "how_to_play")


@dataclass(frozen=True)
class LiveGuide:
    guide_id: int
    name: str  # HSReplay 的英文名："Aberrations - Sludge"
    display: str  # 中文显示名（≤ 12 字）："畸变·梦魇侵蚀怪"
    tribe: str | None  # Race 枚举名；primary_tribe 为 0 或不认识时 None（中立，任何种族都能走）
    tier: int  # 1~3
    tier_rank: int  # 0 起
    difficulty: int  # 1~3
    core_cards: tuple[str, ...]  # 中文卡名，只留 1~6 本的卡池随从，去重保序
    addon_cards: tuple[str, ...]  # 同上
    summary: str  # 卡名已换成中文，英文原文
    when_to_commit: str
    how_to_play: str
    updated: date  # last_updated 的 UTC 日期
    card_tiers: tuple[tuple[str, int], ...] = ()  # 核心卡和配合卡的酒馆等级（段里写"（5 本）"）


@dataclass(frozen=True)
class LiveGuides:
    fetched: datetime  # 缓存文件的修改时间（本地时区）
    patch_date: date
    guides: tuple[LiveGuide, ...]  # 新鲜、不隐藏的，按 (tier, tier_rank, name) 排
    # 文件里不隐藏、读得出来的条目一共几条（= 新鲜的 + 补丁前的；隐藏的和有误的不算，
    # 启动行"全部 N 条，M 条是补丁前的"才加得起来）
    total: int
    stale: int  # 补丁前的几条
    newest: date  # 全部条目里最新的 last_updated
    cached_age: float | None = None  # 用的是旧缓存时缓存多少秒前写的；新下载的为 None


def _int(value: object) -> int | None:
    """整数或整数的字符串（"2"）；都不是时 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _ranged(value: object, default: int) -> int:
    number = _int(value)
    return number if number is not None and 1 <= number <= 3 else default


def _updated(raw: dict) -> date | None:
    text = raw.get("last_updated")
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date()


def _tribe(value: object) -> str | None:
    number = _int(value)
    if not number:
        return None
    try:
        race = Race(number).name
    except ValueError:
        return None
    return race if race in TRIBE_ZH and race != ALL else None


def _text(raw: dict, field: str, cards: CardDB) -> str:
    """卡名引用换成中文名（查不到时留英文名），换行换成" / "，连续空白合一。"""
    value = raw.get(field)
    if not isinstance(value, str):
        return ""

    def name(match: re.Match[str]) -> str:
        info = cards.get_by_dbf(int(match.group(2)))
        return info.name if info is not None else match.group(1).strip()

    lines = (" ".join(line.split()) for line in _REF.sub(name, value).splitlines())
    return " / ".join(line for line in lines if line)


def _cards(raw: dict, field: str, cards: CardDB) -> list[tuple[str, int]]:
    """dbfId 列表 → (卡名, 酒馆等级)：只留 1~6 本的卡池随从，去重保序。"""
    value = raw.get(field)
    kept: dict[str, int] = {}
    for dbf in value if isinstance(value, list) else ():
        number = _int(dbf)
        info = cards.get_by_dbf(number) if number is not None else None
        if info is None or not info.is_pool_minion:
            continue
        if not 1 <= (info.tier or 0) <= MAX_TAVERN_TIER:
            continue
        kept.setdefault(info.name, info.tier or 0)
    return list(kept.items())


def _display(raw: dict, tribe: str | None, cards: CardDB) -> str:
    """显示名"种族中文·代表卡中文名"，超过 PLAN_COMP_CHARS 时截掉代表卡名的末尾；代表卡查不到时
    用英文名里 " - " 后面的部分。"""
    head = f"{tribe_zh(tribe) if tribe else NEUTRAL}·"
    card_id = raw.get("representative_card")
    info = cards.get(card_id) if isinstance(card_id, str) and card_id else None
    label = info.name if info is not None else str(raw["name"]).split(" - ", 1)[-1].strip()
    return head + label[: max(PLAN_COMP_CHARS - len(head), 1)]


def _unique(display: str, used: set[str]) -> str:
    """显示名和前面的重复时末尾加 2、3……（仍不超过 PLAN_COMP_CHARS）。"""
    name, number = display, 2
    while name in used:
        suffix = str(number)
        name = display[: PLAN_COMP_CHARS - len(suffix)] + suffix
        number += 1
    used.add(name)
    return name


def _guide(raw: dict, cards: CardDB, updated: date) -> LiveGuide:
    guide_id, name = raw["id"], raw["name"]
    if isinstance(guide_id, bool) or not isinstance(guide_id, int):
        raise TypeError("id 不是整数")
    if not isinstance(name, str) or not name.strip():
        raise TypeError("name 不是字符串")
    if not isinstance(raw["core_cards"], list):
        raise TypeError("core_cards 不是列表")
    tribe = _tribe(raw.get("primary_tribe"))
    core, addon = _cards(raw, "core_cards", cards), _cards(raw, "addon_cards", cards)
    rank = _int(raw.get("tier_rank"))
    return LiveGuide(
        guide_id=guide_id,
        name=name.strip(),
        display=_display(raw, tribe, cards),
        tribe=tribe,
        tier=_ranged(raw.get("tier"), DEFAULT_TIER),
        tier_rank=rank if rank is not None else 0,
        difficulty=_ranged(raw.get("difficulty"), DEFAULT_DIFFICULTY),
        core_cards=tuple(card for card, _ in core),
        addon_cards=tuple(card for card, _ in addon),
        summary=_text(raw, "summary", cards),
        when_to_commit=_text(raw, "when_to_commit", cards),
        how_to_play=_text(raw, "how_to_play", cards),
        updated=updated,
        card_tiers=tuple(dict(core + addon).items()),
    )


def load_live_guides(
    path: Path, patch_date: date, cards: CardDB, tz: tzinfo | None = None
) -> LiveGuides:
    """读缓存的流派指南。不是列表时抛 ValueError；字段缺失、类型不对的条目跳过并记调试日志；
    last_updated 解析不了的当作补丁前。tz：显示时间用的时区（None 表示本机的时区）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("流派指南格式不对")
    fresh: list[LiveGuide] = []
    stale = 0
    dates: list[date] = []
    for raw in data:
        try:
            if not isinstance(raw, dict):
                raise TypeError("不是对象")
            updated = _updated(raw)
            if updated is not None:
                dates.append(updated)
            if raw.get("hidden") is True:
                continue
            guide = _guide(raw, cards, updated or date.min)
        except (KeyError, TypeError, ValueError, AttributeError):
            log.debug("跳过一条有误的流派指南：%r", raw, exc_info=True)
            continue
        if updated is None or updated < patch_date:
            stale += 1
        else:
            fresh.append(guide)
    fresh.sort(key=lambda g: (g.tier, g.tier_rank, g.name))
    used: set[str] = set()
    guides = tuple(replace(g, display=_unique(g.display, used)) for g in fresh)
    mtime = path.stat().st_mtime
    if tz is None:
        fetched = datetime.fromtimestamp(mtime).astimezone()
    else:
        fetched = datetime.fromtimestamp(mtime, tz)
    return LiveGuides(
        fetched=fetched,
        patch_date=patch_date,
        guides=guides,
        total=len(guides) + stale,
        stale=stale,
        newest=max(dates, default=patch_date),
    )


def _with_tiers(names: tuple[str, ...], tiers: dict[str, int]) -> str:
    return "、".join(f"{name}（{tiers[name]} 本）" if name in tiers else name for name in names)


def _body(guide: LiveGuide, ranked: bool) -> str:
    """段的正文：强度、一句话、核心卡、配合卡、什么时候走、打法要点、英文名（空的不写）。
    ranked：这一条的 tier_rank 在同一梯队里是唯一的，才写"第 N 位"（HSReplay 的 tier_rank
    常有并列）。"""
    tiers = dict(guide.card_tiers)
    difficulty = DIFFICULTY_ZH.get(guide.difficulty, DIFFICULTY_ZH[DEFAULT_DIFFICULTY])
    rank = f"第 {guide.tier_rank + 1} 位" if ranked else ""
    lines = [
        f"- 强度：第 {guide.tier} 梯队{rank}；难度 {difficulty}；"
        f"{SOURCE} {guide.updated:%m-%d} 更新"
    ]
    if guide.summary:
        lines.append(f"- 一句话：{guide.summary}")
    if guide.core_cards:
        lines.append(f"- 核心卡：{_with_tiers(guide.core_cards, tiers)}")
    if guide.addon_cards:
        lines.append(f"- 配合卡：{_with_tiers(guide.addon_cards, tiers)}")
    if guide.when_to_commit:
        lines.append(f"- 什么时候走：{guide.when_to_commit}")
    if guide.how_to_play:
        lines.append(f"- 打法要点（英文原文）：{guide.how_to_play}")
    lines.append(f"- 英文名：{guide.name}")
    return "\n".join(lines)


def _season(manual: CompsFile | None) -> str:
    """comps.md 标题开头的赛季（"# 第 14 赛季流派指南（……）" → "第 14 赛季"）；没有时"本赛季"。"""
    title = manual.title if manual is not None else ""
    head = re.split(r"[（(]", title, maxsplit=1)[0].strip().removesuffix("流派指南").strip()
    return head or SEASON


def to_comps_file(live: LiveGuides, manual: CompsFile | None) -> CompsFile:
    """HSReplay 的新鲜指南写成流派指南的段（按梯队排好），comps.md 的通用运营段接在最后；
    块的上限是 LIVE_MAX_CHARS。"""
    ranks = Counter((g.tier, g.tier_rank) for g in live.guides)
    guides = [
        CompGuide(
            name=g.display,
            tribes=frozenset({g.tribe}) if g.tribe is not None else frozenset({ALL}),
            body=_body(g, ranks[(g.tier, g.tier_rank)] == 1),
            core_cards=g.core_cards,
            aliases=(g.name,),
            tier=g.tier,
            difficulty=g.difficulty,
        )
        for g in live.guides
    ]
    if manual is not None:
        guides += [g for g in manual.guides if g.general]
    title = (
        f"{_season(manual)}流派指南（数据来自 {SOURCE}，{live.newest:%m-%d} 更新，"
        f"{len(live.guides)} 条本补丁的指南；强度写梯队，同一梯队里新手优先难度低的；"
        "打法要点是英文原文）"
    )
    return CompsFile(title, tuple(guides), max_chars=LIVE_MAX_CHARS)
