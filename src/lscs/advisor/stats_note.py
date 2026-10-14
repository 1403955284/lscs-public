"""本版本统计的条目（本版本统计设计文档第 6.1、7.1 节）：一组备选英雄或饰品选项各自的平均名次和
被选率。文字模板放在这里，终端、面板和规则共用一份。只做相对比较（这几个之间比），所以
"战绩最好"只在样本够的条目里挑；样本少的照样列出、标"样本少"。
"""

from __future__ import annotations

from collections.abc import Sequence

from lscs.advisor.models import StatRow, StatsNote
from lscs.state.models import Choice, ChoiceKind, HeroSelect
from lscs.stats.model import HeroStat, MetaStats, TrinketStat

LOW_SAMPLE = "样本少"


def pct(rate: float) -> str:
    """被选率：0.2836 → "28%"。"""
    return f"{rate * 100:.0f}%"


def hero_row_text(stat: HeroStat, total: int) -> str:
    """ "第 3.67 名 · 选 28% · 第 5/116"；样本少："第 4.20 名（样本少）"。"""
    if stat.low or stat.rank is None:
        return f"第 {stat.average_position:.2f} 名（{LOW_SAMPLE}）"
    return f"第 {stat.average_position:.2f} 名 · 选 {pct(stat.pick_rate)} · 第 {stat.rank}/{total}"


def trinket_row_text(stat: TrinketStat) -> str:
    """ "第 3.89 名 · 选 27%"；样本少："第 3.50 名（样本少）"。"""
    if stat.low:
        return f"第 {stat.average_placement:.2f} 名（{LOW_SAMPLE}）"
    return f"第 {stat.average_placement:.2f} 名 · 选 {pct(stat.pick_rate)}"


def _best(rows: Sequence[StatRow]) -> int | None:
    """平均名次最好的（样本少的不参与；并列取靠前的）。"""
    usable = [(row.score, i, row.entity_id) for i, row in enumerate(rows) if not row.low]
    return min(usable)[2] if usable else None


def hero_note(select: HeroSelect, stats: MetaStats) -> StatsNote | None:
    """每个查得到的备选英雄一条（按画面顺序）；一个都查不到时为 None。"""
    rows = []
    for hero in select.offered:
        stat = stats.hero(hero.card_id)
        if stat is None:
            continue
        text = hero_row_text(stat, stats.hero_total)
        rows.append(
            StatRow(entity_id=hero.entity_id, text=text, score=stat.average_position, low=stat.low)
        )
    if not rows:
        return None
    return StatsNote(
        kind="hero",
        signature=select.signature(),
        rows=tuple(rows),
        best=_best(rows),
        source=stats.source_text(),
    )


def trinket_note(choice: Choice, stats: MetaStats) -> StatsNote | None:
    """饰品选项每个查得到的一条（按画面顺序）；不是饰品、一个都查不到时为 None。"""
    if choice.kind != ChoiceKind.TRINKET:
        return None
    rows = []
    for option in choice.options:
        stat = stats.trinket(option.card_id)
        if stat is None:
            continue
        rows.append(
            StatRow(
                entity_id=option.entity_id,
                text=trinket_row_text(stat),
                score=stat.average_placement,
                low=stat.low,
            )
        )
    if not rows:
        return None
    return StatsNote(
        kind="trinket",
        choice_id=choice.id,
        rows=tuple(rows),
        best=_best(rows),
        source=stats.source_text(),
    )
