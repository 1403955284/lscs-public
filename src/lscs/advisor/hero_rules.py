"""选英雄的规则建议（选英雄建议设计文档第 4 节）：只看护甲，技能怎么样交给 AI。

暴雪用起始护甲平衡英雄：越强的英雄给的护甲越少。所有备选英雄都有护甲、且不全相同时，
推荐护甲最少的（并列取位置靠前的）；有英雄没有护甲数据（2022 年的日志）或护甲全一样时
不推荐，等 AI 建议。每个英雄的点评就是它的护甲。

打开本版本统计（本版本统计设计文档第 6.2 节）、每个备选英雄都有可靠的统计时，改成推荐本版本
平均名次最好的（并列取护甲少的，再并列取位置靠前的），点评是每个英雄的统计；有英雄查不到或
样本少时和以前一样按护甲。
"""

from __future__ import annotations

from lscs.advisor.models import HeroAdvice, HeroComment
from lscs.advisor.names import armor_zh
from lscs.advisor.stats_note import hero_row_text
from lscs.state.models import HeroSelect
from lscs.stats.model import MetaStats

LOWEST_ARMOR = "这几个英雄里它护甲最少：官方给越强的英雄发越少的护甲"
ARMOR_MISSING = "护甲信息不全，等 AI 建议"
BEST_STATS = (
    "这几个英雄里它本版本平均名次最好（第 {avg:.2f} 名，{total} 个英雄里第 {rank}；"
    "数据来自 Firestone）"
)
NO_ARMOR = 99  # 平均名次并列时比护甲：没有护甲数据的排在后面


def _same_armor(count: int) -> str:
    return "四个英雄护甲一样，等 AI 建议" if count == 4 else "备选英雄护甲都一样，等 AI 建议"


def _stats_pick(select: HeroSelect, stats: MetaStats) -> HeroAdvice | None:
    """每个备选英雄都有统计、都不是样本少时按平均名次推荐；否则为 None（按护甲）。"""
    offered = select.offered
    found = [(h, stats.hero(h.card_id)) for h in offered]
    pairs = [(h, s) for h, s in found if s is not None and not s.low and s.rank is not None]
    if not offered or len(pairs) != len(offered):
        return None
    best, stat = min(
        pairs, key=lambda p: (p[1].average_position, p[0].armor or NO_ARMOR, p[0].position)
    )
    total = stats.hero_total
    comments = tuple(
        HeroComment(entity_id=h.entity_id, text=hero_row_text(s, total)) for h, s in pairs
    )
    return HeroAdvice(
        source="rules",
        signature=select.signature(),
        pick=best.entity_id,
        reason=BEST_STATS.format(avg=stat.average_position, total=total, rank=stat.rank),
        comments=comments,
        basis="stats",
    )


def rule_hero_pick(select: HeroSelect, stats: MetaStats | None = None) -> HeroAdvice:
    """stats：本版本统计（关闭、还没准备好时为 None）。"""
    if stats is not None:
        advice = _stats_pick(select, stats)
        if advice is not None:
            return advice
    offered = select.offered
    comments = tuple(HeroComment(entity_id=h.entity_id, text=armor_zh(h.armor)) for h in offered)
    pick, reason = None, ARMOR_MISSING
    if offered and all(h.armor > 0 for h in offered):
        if len({h.armor for h in offered}) == 1:
            reason = _same_armor(len(offered))
        else:
            best = min(offered, key=lambda h: (h.armor, h.position))
            pick, reason = best.entity_id, LOWEST_ARMOR
    return HeroAdvice(
        source="rules", signature=select.signature(), pick=pick, reason=reason, comments=comments
    )
