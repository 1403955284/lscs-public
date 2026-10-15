"""整局计划的写法和"计划提醒"（整局计划与流派指南设计文档第 5.2、7.2、8 节）。"""

from factory import hand_minion, make_state, minion, shop_minion, shop_spell

from lscs.advisor.context import build_context
from lscs.advisor.models import Action, GamePlan, Plan
from lscs.advisor.plan_check import (
    comp_with_tribe,
    held_names,
    key_cards_text,
    off_plan_note,
    plan_look_for,
)

NAMES = {
    "BG36_099": "蚀脑怪",
    "BG36_099_G": "蚀脑怪(金)",
    "BG36_113": "飘荡祭品",
    "BG36_113_G": "飘荡祭品(金)",
    "BGS_061": "海盗无赖",
    "S": "香蕉",
}


def name(card_id):
    return NAMES.get(card_id, card_id)


PLAN = GamePlan(comp="畸变神明", tribe="ABERRATION", key_cards=("蚀脑怪", "飘荡祭品"))
NOTE = "商店里有核心卡 飘荡祭品，方案却没买它"


def test_held_key_cards_are_marked_including_golden_ones_and_hand_minions():
    golden = make_state(board=[minion(10, "BG36_099_G", 6, 8)])
    assert held_names(golden, name) == frozenset({"蚀脑怪"})
    assert key_cards_text(PLAN, golden, name) == "蚀脑怪✓、飘荡祭品"
    in_hand = make_state(hand=[hand_minion(30, "BG36_113", 2, 1)])
    assert key_cards_text(PLAN, in_hand, name) == "蚀脑怪、飘荡祭品✓"
    assert key_cards_text(PLAN, None, name) == "蚀脑怪、飘荡祭品"
    assert key_cards_text(PLAN.model_copy(update={"key_cards": ()}), golden, name) == ""


def test_comp_with_tribe():
    assert comp_with_tribe(PLAN) == "畸变神明"  # 名字里已经有种族
    assert comp_with_tribe(PLAN, always=True) == "畸变神明（畸变）"
    blades = GamePlan(comp="刀剑流", tribe="PIRATE")
    assert comp_with_tribe(blades) == "刀剑流（海盗）"
    assert comp_with_tribe(GamePlan(comp="海盗", tribe="PIRATE"), always=True) == "海盗"
    assert comp_with_tribe(GamePlan(comp="未定", tribe="PIRATE"), always=True) == "未定"
    assert comp_with_tribe(GamePlan(comp="随便")) == "随便"


def test_plan_look_for_adds_the_key_cards_once():
    assert plan_look_for(("飘荡祭品", "神明"), PLAN) == ("飘荡祭品", "神明", "蚀脑怪")
    assert plan_look_for(("神明",), None) == ("神明",)


def note_for(plan_actions, gold=5, board=(), hand=(), shop=None, game_plan=PLAN):
    shop = shop if shop is not None else [shop_minion(20, "BG36_113", 2, 1, tier=3)]
    state = make_state(gold=gold, board=board, hand=hand, shop=shop)
    ctx = build_context(state, 3)
    plan = Plan(source="llm", actions=tuple(plan_actions))
    return off_plan_note(plan, game_plan, ctx, state, name)


def test_key_card_in_the_shop_that_the_plan_skips():
    assert note_for([Action(type="refresh")]) == NOTE
    assert note_for([]) == NOTE  # "无需操作"也算


def test_no_note_when_the_plan_buys_it():
    assert note_for([Action(type="buy", entity_id=20), Action(type="refresh")]) is None
    two = [shop_minion(20, "BG36_113", 2, 1), shop_minion(21, "BG36_113", 2, 1)]
    assert note_for([Action(type="buy", entity_id=21)], shop=two) is None  # 买了其中一张


def test_no_note_when_the_key_card_is_not_affordable():
    assert note_for([Action(type="refresh")], gold=2) is None


def test_no_note_when_the_board_is_full_unless_the_plan_sells():
    full = [minion(10 + i, "BGS_061", 3, 1) for i in range(7)]
    assert note_for([Action(type="refresh")], board=full) is None
    sell = [Action(type="sell", entity_id=10), Action(type="refresh")]
    assert note_for(sell, board=full) == NOTE


def test_no_note_for_golden_copies_other_cards_or_without_a_plan():
    golden = [shop_minion(20, "BG36_113_G", 4, 2, golden=True)]
    assert note_for([], shop=golden) is None
    others = [shop_minion(20, "BGS_061", 3, 1), shop_spell(21, "S", cost=1)]
    assert note_for([], shop=others) is None
    assert note_for([], game_plan=None) is None
    assert note_for([], game_plan=PLAN.model_copy(update={"key_cards": ()})) is None


def test_the_first_key_card_in_plan_order_is_named():
    shop = [shop_minion(20, "BG36_113", 2, 1), shop_minion(21, "BG36_099", 3, 4)]
    assert note_for([], shop=shop) == "商店里有核心卡 蚀脑怪，方案却没买它"


def test_plain_name_drops_display_and_golden_marks():
    from lscs.advisor.plan_check import plain_name

    assert [plain_name(t) for t in ("蚀脑怪✓", " 蚀脑怪(金) ", "蚀脑怪(金)✓", "蚀脑怪✔")] == [
        "蚀脑怪"
    ] * 4


# ---- 计划提醒更准（评审修正）：已经有的、升本的、照方案做完买不起或放不下的都不算 ----


def test_no_note_for_a_key_card_already_held():
    on_board = [minion(10, "BG36_113", 2, 1)]
    assert note_for([Action(type="refresh")], board=on_board) is None
    in_hand = [hand_minion(30, "BG36_113_G", 4, 2, golden=True)]
    assert note_for([Action(type="refresh")], hand=in_hand) is None
    # 已经有第一张核心卡时照样提第二张
    shop = [shop_minion(20, "BG36_113", 2, 1), shop_minion(21, "BG36_099", 3, 4)]
    brain = [minion(10, "BG36_099", 3, 4)]
    assert note_for([], shop=shop, board=brain) == NOTE


def test_no_note_and_no_deviation_when_the_plan_upgrades():
    assert note_for([Action(type="upgrade")], gold=9) is None
    assert note_for([Action(type="refresh"), Action(type="upgrade")], gold=9) is None


def test_no_note_when_the_other_steps_spend_the_gold():
    shop = [shop_minion(20, "BG36_113", 2, 1), shop_minion(21, "BGS_061", 3, 1)]
    buy_other = [Action(type="buy", entity_id=21)]
    assert note_for(buy_other, gold=5, shop=shop) is None  # 买完只剩 2
    assert note_for(buy_other, gold=6, shop=shop) == NOTE  # 还剩 3
    assert note_for([Action(type="refresh")], gold=3) is None  # 刷新 1 金后只剩 2
    assert note_for([Action(type="refresh")], gold=4) == NOTE
    sell = [Action(type="sell", entity_id=10), Action(type="refresh")]
    assert note_for(sell, gold=3, board=[minion(10, "BGS_061", 3, 1)]) == NOTE  # 卖 +1


def test_no_note_when_the_plan_fills_the_board():
    six = [minion(10 + i, "BGS_061", 3, 1) for i in range(6)]
    shop = [shop_minion(20, "BG36_113", 2, 1), shop_minion(21, "BGS_061", 3, 1)]
    assert note_for([Action(type="buy", entity_id=21)], gold=9, board=six, shop=shop) is None
    assert note_for([Action(type="refresh")], gold=9, board=six, shop=shop) == NOTE
    seven = [*six, minion(16, "BGS_061", 3, 1)]
    assert note_for([], gold=9, board=seven, shop=shop) is None  # 满了，方案也不卖
