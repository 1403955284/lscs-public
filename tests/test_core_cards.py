"""商店核心卡标记的规则（流派指南自动更新设计文档第 5.1 节）。"""

from factory import make_state, shop_minion, shop_spell

from lscs.advisor.core_cards import CORE, MAX_CORE_NAMES, CoreMark, core_marks, core_names
from lscs.advisor.models import GamePlan
from lscs.llm.comps import CompGuide, CompsFile

NAMES = {
    "A1": "甲虫",
    "A1_G": "甲虫(金)",
    "B1": "乙鱼",
    "C1": "丙龙",
    "D1": "丁魔",
    "E1": "戊兽",
    "S1": "法术甲虫",
}


def name(card_id):
    return NAMES.get(card_id, card_id)


def guide(title, tribes, *cards):
    return CompGuide(title, frozenset(tribes), "- 要点", core_cards=tuple(cards))


COMPS = CompsFile(
    "标题",
    (
        guide("中立·融合", {"ALL"}, "甲虫", "丁魔"),
        guide("鱼人·一号", {"MURLOC"}, "乙鱼", "甲虫"),
        guide("龙·二号", {"DRAGON"}, "丙龙", "乙鱼"),
        guide("鱼人·三号", {"MURLOC"}, "乙鱼", "戊兽"),
        guide("野兽·四号", {"BEAST"}, "戊兽"),
        CompGuide("通用运营（本赛季）", None, "- 前期升本", core_cards=("己",)),
    ),
)
LOBBY = frozenset({"MURLOC", "DRAGON"})


def shop_state(*items, turn=9, tribes=LOBBY):
    return make_state(turn=turn, shop=items, tribes=tribes)


def test_symbol_and_cap():
    assert CORE == "◆" and MAX_CORE_NAMES == 3


def test_plan_key_cards_come_first():
    plan = GamePlan(comp="畸变神明", key_cards=("蚀脑怪", "飘荡祭品"))
    assert core_names(plan, COMPS, LOBBY) == (("蚀脑怪", "计划"), ("飘荡祭品", "计划"))
    undecided = GamePlan(comp="未定", key_cards=("海藻护卫",))  # "未定"时写的"想找"的卡也算
    assert core_names(undecided, None, LOBBY) == (("海藻护卫", "计划"),)


def test_without_a_plan_the_selected_comps_core_cards_by_count_then_order():
    names = core_names(GamePlan(comp="未定"), COMPS, LOBBY)  # 没有核心卡的计划：看指南
    assert names == (
        ("乙鱼", "鱼人·一号、龙·二号"),  # 三段里都有：最多两个流派名
        ("甲虫", "中立·融合、鱼人·一号"),
        ("丁魔", "中立·融合"),
        ("丙龙", "龙·二号"),
        ("戊兽", "鱼人·三号"),  # 野兽·四号不在本局；通用运营的不算
    )
    assert core_names(None, COMPS, frozenset()) == ()  # 种族还不知道：不标


def test_nothing_without_a_plan_or_guides():
    assert core_names(None, None, LOBBY) == ()
    assert core_marks(shop_state(shop_minion(1, "A1", 1, 1)), None, None, name) is None
    assert (
        core_marks(shop_state(shop_minion(1, "A1", 1, 1), tribes=frozenset()), None, COMPS, name)
        is None
    )


def test_marks_in_shop_order_including_golden_and_duplicates_but_not_spells():
    state = shop_state(
        shop_minion(10, "C1", 1, 1),
        shop_spell(11, "S1", 1),
        shop_minion(12, "A1_G", 2, 2, golden=True),
        shop_minion(13, "X9", 1, 1),
        shop_minion(14, "C1", 1, 1),
    )
    marks = core_marks(state, None, COMPS, name)
    assert marks.source == "guides" and marks.turn == 9
    assert marks.shop == (10, 11, 12, 13, 14)
    assert marks.marks == (
        CoreMark(entity_id=10, index=0, name="丙龙", why="龙·二号"),
        CoreMark(entity_id=12, index=2, name="甲虫", why="中立·融合、鱼人·一号"),
        CoreMark(entity_id=14, index=4, name="丙龙", why="龙·二号"),
    )
    assert marks.names == ("乙鱼", "甲虫", "丁魔", "丙龙", "戊兽")


def test_at_most_three_names_are_marked_without_a_plan():
    state = shop_state(
        shop_minion(1, "E1", 1, 1),
        shop_minion(2, "C1", 1, 1),
        shop_minion(3, "D1", 1, 1),
        shop_minion(4, "A1", 1, 1),
        shop_minion(5, "D1", 1, 1),
    )
    marks = core_marks(state, None, COMPS, name)
    # 四个名字：按（在几段里、第一次出现的段）取 甲虫、丁魔、丙龙；同名的两张都标
    assert [m.entity_id for m in marks.marks] == [2, 3, 4, 5]
    assert {m.name for m in marks.marks} == {"甲虫", "丁魔", "丙龙"}


def test_plan_marks_every_key_card_in_the_shop():
    plan = GamePlan(comp="畸变神明", key_cards=("甲虫", "丙龙", "丁魔", "戊兽"))
    state = shop_state(*(shop_minion(i, c, 1, 1) for i, c in enumerate(["A1", "C1", "D1", "E1"])))
    marks = core_marks(state, plan, COMPS, name)
    assert marks.source == "plan" and len(marks.marks) == 4  # 计划的卡不受上限
    assert {m.why for m in marks.marks} == {"计划"}


def test_names_without_marks_still_return_the_marks_for_the_choices():
    marks = core_marks(shop_state(shop_minion(1, "X9", 1, 1)), None, COMPS, name)
    assert marks is not None and marks.marks == () and "乙鱼" in marks.names
    empty = core_marks(shop_state(), GamePlan(comp="甲", key_cards=("甲虫",)), None, name)
    assert empty.marks == () and empty.shop == () and empty.names == ("甲虫",)


def test_comps_that_do_not_fit_in_the_block_are_not_marked():
    """流派指南块放不下的流派（AI 没收到）：它的核心卡不标。"""
    from dataclasses import replace

    from lscs.llm.comps import comps_block

    full = comps_block(COMPS, LOBBY)
    # 只放得下标题、通用运营和前两个流派（中立·融合、鱼人·一号）
    limit = full.index("\n## 龙·二号") + len("\n## 通用运营（本赛季）\n- 前期升本")
    small = replace(COMPS, max_chars=limit)
    block = comps_block(small, LOBBY)
    assert "## 鱼人·一号" in block and "## 龙·二号" not in block and "放不下" in block
    assert core_names(None, small, LOBBY) == (
        ("甲虫", "中立·融合、鱼人·一号"),
        ("丁魔", "中立·融合"),
        ("乙鱼", "鱼人·一号"),  # 龙·二号、鱼人·三号放不下：不算
    )
    state = shop_state(shop_minion(1, "C1", 1, 1), shop_minion(2, "B1", 1, 1))
    marks = core_marks(state, None, small, name)
    assert [m.name for m in marks.marks] == ["乙鱼"]  # 丙龙只在放不下的龙·二号里
    assert "丙龙" not in marks.names
