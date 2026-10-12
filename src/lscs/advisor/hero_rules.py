"""选英雄的规则建议（选英雄建议设计文档第 4 节）：只看护甲，技能怎么样交给 AI。

暴雪用起始护甲平衡英雄：越强的英雄给的护甲越少。所有备选英雄都有护甲、且不全相同时，
推荐护甲最少的（并列取位置靠前的）；有英雄没有护甲数据（2022 年的日志）或护甲全一样时
不推荐，等 AI 建议。每个英雄的点评就是它的护甲。
"""

from __future__ import annotations

from lscs.advisor.models import HeroAdvice, HeroComment
from lscs.advisor.names import armor_zh
from lscs.state.models import HeroSelect

LOWEST_ARMOR = "这几个英雄里它护甲最少：官方给越强的英雄发越少的护甲"
ARMOR_MISSING = "护甲信息不全，等 AI 建议"


def _same_armor(count: int) -> str:
    return "四个英雄护甲一样，等 AI 建议" if count == 4 else "备选英雄护甲都一样，等 AI 建议"


def rule_hero_pick(select: HeroSelect) -> HeroAdvice:
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
