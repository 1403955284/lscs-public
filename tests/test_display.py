from factory import hand_spell, make_state, minion, power, shop_minion, shop_spell

from lscs.advisor.display import AdviceDisplay, DisplayGroup, describe_action, format_plan
from lscs.advisor.models import Action, Advice, HeroAdvice, HeroComment, Plan
from lscs.state.models import HeroOption, HeroSelect, Phase

NAMES = {"A": "小鬼", "B": "海盗", "S": "香蕉", "BG21_HERO_000p": "定罪", "AIM": "瞄准"}


def name(card_id):
    return NAMES.get(card_id, card_id)


STATE = make_state(
    tier=2,
    board=[minion(10, "A", 1, 1)],
    shop=[shop_minion(20, "B", 3, 2), shop_spell(30, "S", cost=1)],
)


def test_describe_each_action_type():
    assert describe_action(Action(type="buy", entity_id=20, reason="好"), STATE, name) == (
        "买 海盗 3/2（好）"
    )
    assert describe_action(Action(type="buy", entity_id=30), STATE, name) == "买 香蕉（法术，1 金）"
    assert describe_action(Action(type="buy", entity_id=99), STATE, name) == "买 #99"
    assert describe_action(Action(type="sell", entity_id=10, reason="弱"), STATE, name) == (
        "卖 小鬼 1/1（弱）"
    )
    assert (
        describe_action(Action(type="upgrade", reason="节奏"), STATE, name) == "升本 → 3 本（节奏）"
    )
    assert describe_action(Action(type="refresh"), STATE, name) == "刷新"
    assert describe_action(Action(type="freeze", reason="留牌"), STATE, name) == "冻结（留牌）"


def test_format_plan_numbers_steps_and_handles_empty_plan():
    plan = Plan(source="rules", actions=(Action(type="buy", entity_id=20), Action(type="refresh")))
    assert format_plan(plan, STATE, name) == ["① 买 海盗 3/2", "② 刷新"]
    empty = Plan(source="rules", summary="本回合不需要操作")
    assert format_plan(empty, STATE, name) == ["无需操作（本回合不需要操作）"]


def test_show_rules_prints_block_once_per_plan():
    out = []
    display = AdviceDisplay(name, out.append)
    plan = Plan(
        source="rules",
        actions=(Action(type="buy", entity_id=20, reason="好"), Action(type="refresh")),
        direction="海盗",
    )
    advice = Advice(state_seq=1, turn=3, plan=plan)
    display.show_rules(STATE, advice)
    display.show_rules(STATE, advice)  # 同一方案不重复打印
    assert out == ["  📋 规则：① 买 海盗 3/2（好）", "          ② 刷新   方向：海盗"]


def test_show_rules_marks_uncertain_state():
    out = []
    state = STATE.model_copy(update={"uncertain": True})
    AdviceDisplay(name, out.append).show_rules(
        state,
        Advice(state_seq=1, turn=3, plan=Plan(source="rules", actions=(Action(type="refresh"),))),
    )
    assert out[-1] == "          ⚠ 状态可能不准"


def test_format_plan_without_reasons():
    plan = Plan(
        source="rules",
        actions=(
            Action(type="buy", entity_id=20, reason="好"),
            Action(type="upgrade", reason="节奏"),
        ),
    )
    assert format_plan(plan, STATE, name, reasons=False) == ["① 买 海盗 3/2", "② 升本 → 3 本"]
    assert (
        describe_action(Action(type="freeze", reason="留牌"), STATE, name, reasons=False) == "冻结"
    )
    empty = Plan(source="rules", summary="本回合不需要操作")
    assert format_plan(empty, STATE, name, reasons=False) == ["无需操作"]


class Recorder:
    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        return lambda *args: self.calls.append((method, args))


class Broken:
    def __getattr__(self, method):
        def boom(*_args):
            raise RuntimeError(f"{method} 坏了")

        return boom


def test_display_group_forwards_every_method_and_isolates_errors(caplog):
    from lscs.advisor.display import DisplayGroup

    first, last = Recorder(), Recorder()
    group = DisplayGroup(first, Broken(), last)
    advice = Advice(state_seq=1, turn=3, plan=Plan(source="rules"))
    group.show_rules(STATE, advice)
    group.rules_error()
    group.rules_error(True)
    group.ai_pending()
    group.ai_pending("deep")
    group.ai_text("AI 超时")
    group.ai_text("深度分析超时", "deep")
    group.show_ai(STATE, advice, True)
    group.ai_summary(2, 300, 40)
    expected = [
        ("show_rules", (STATE, advice)),
        ("rules_error", (False,)),
        ("rules_error", (True,)),
        ("ai_pending", ("fast",)),
        ("ai_pending", ("deep",)),
        ("ai_text", ("AI 超时", "fast")),
        ("ai_text", ("深度分析超时", "deep")),
        ("show_ai", (STATE, advice, True)),
        ("ai_summary", (2, 300, 40)),
    ]
    assert first.calls == expected
    assert last.calls == expected
    assert "show_rules 坏了" in caplog.text and "ai_summary 坏了" in caplog.text


def test_describe_hero_power():
    state = make_state(hero_power_state=power())
    action = Action(type="hero_power", reason="免费")
    assert describe_action(action, state, name) == "使用英雄技能 定罪（免费）"
    # 不需要目标时 AI 多给的编号不显示
    assert describe_action(Action(type="hero_power", entity_id=10), state, name) == (
        "使用英雄技能 定罪"
    )
    assert describe_action(action, make_state(), name, reasons=False) == "使用英雄技能"


def test_describe_hero_power_with_target():
    state = make_state(
        board=[minion(10, "A", 1, 1)],
        shop=[shop_minion(20, "B", 3, 2)],
        hand=[hand_spell(30, "S")],
        hero_power_state=power("AIM", targets=(10, 20, 30, 40)),
    )

    def aimed(target):
        return describe_action(Action(type="hero_power", entity_id=target), state, name)

    assert aimed(10) == "使用英雄技能 瞄准 → 小鬼 1/1"
    assert aimed(20) == "使用英雄技能 瞄准 → 海盗 3/2"
    assert aimed(30) == "使用英雄技能 瞄准 → 香蕉"
    assert aimed(40) == "使用英雄技能 瞄准 → #40"


def test_describe_hero_power_target_when_requirement_is_unknown():
    hp = power("AIM", cost=2, option="unusable", reason="REQ_ENOUGH_MANA", needs_target=None)
    state = make_state(board=[minion(10, "A", 1, 1)], hero_power_state=hp)
    action = Action(type="hero_power", entity_id=10)
    assert describe_action(action, state, name) == "使用英雄技能 瞄准 → 小鬼 1/1"


def test_show_ai_lines_for_analysis_direction_and_look_for():
    out = []
    display = AdviceDisplay(name, out.append)
    plan = Plan(
        source="llm",
        actions=(Action(type="buy", entity_id=20, reason="对子"),),
        summary="买海盗",
        analysis="中期，血量 20，\n准备走海盗。",
        direction="海盗",
        look_for=("船长", "海盗无赖"),
    )
    display.show_ai(STATE, Advice(state_seq=1, turn=3, plan=plan, elapsed=2.14), False)
    assert out == [
        "  🤖 AI（2.1 秒）：① 买 海盗 3/2（对子）",
        "          分析：中期，血量 20， 准备走海盗。",  # 完整显示，换行变成空格
        "          方向：海盗   要找：船长、海盗无赖",
    ]


def test_show_ai_without_steps_does_not_repeat_the_summary():
    out = []
    plan = Plan(source="llm", summary="省钱", look_for=("船长",))
    AdviceDisplay(name, out.append).show_ai(
        STATE, Advice(state_seq=1, turn=3, plan=plan, elapsed=1.0), False
    )
    assert out == ["  🤖 AI（1.0 秒）：无需操作（省钱）", "          要找：船长"]


def test_deep_pending_and_text():
    out = []
    display = AdviceDisplay(name, out.append)
    display.ai_pending("deep")
    display.ai_text("深度分析超时", "deep")
    display.ai_pending()
    assert out == ["  🧠 深度分析中…（约 20 秒）", "  🧠 深度分析超时", "  🤖 AI 分析中…"]


def test_deep_agreeing_with_rules_still_shows_the_analysis():
    out = []
    plan = Plan(source="llm", analysis="照规则来。", direction="海盗")
    advice = Advice(state_seq=1, turn=3, plan=plan, elapsed=23.4, lane="deep")
    AdviceDisplay(name, out.append).show_ai(STATE, advice, True)
    assert out == [
        "  🧠 ✓ 深度分析同意规则建议（23 秒）",
        "          分析：照规则来。",
        "          方向：海盗",
    ]


def test_expired_deep_labels_say_why():
    plan = Plan(source="llm", analysis="照规则来。")
    heads = []
    for cause in ("refreshed", "invalid", "changed"):
        out = []
        advice = Advice(state_seq=1, turn=3, plan=plan, lane="deep", expired=cause)
        AdviceDisplay(name, out.append).show_ai(STATE, advice, False)
        heads.append(out[0])
        assert out[1:] == ["          分析：照规则来。"]
    assert heads == [
        "  🧠 深度分析：操作已过期（商店刷新了），只看思路",
        "  🧠 深度分析：操作不可用，只看思路",
        "  🧠 深度分析：操作已过期，只看思路",
    ]


def test_deep_wait_hint_follows_the_thinking_setting():
    from lscs.advisor.display import deep_wait_hint

    assert (deep_wait_hint("low"), deep_wait_hint("high")) == ("约 20 秒", "约 40 秒")
    out = []
    AdviceDisplay(name, out.append, deep_thinking="high").ai_pending("deep")
    assert out == ["  🧠 深度分析中…（约 40 秒）"]


HERO_NAMES = {
    "H1": "凯瑞尔·罗姆",
    "H2": "泰瑟兰·血望者",
    "H3": "洛，在世传奇",
    "H4": "了不起的杰弗里斯",
}
HEROES = HeroSelect(
    offered=tuple(
        HeroOption(
            entity_id=88 + i,
            card_id=f"H{i}",
            position=i,
            armor=armor,
            health=30,
            hero_power_dbf=None,
        )
        for i, armor in enumerate((10, 18, 19, 17), start=1)
    )
)
HERO_STATE = make_state(turn=0, phase=Phase.HERO_SELECT, hero="TB_BaconShop_HERO_PH").model_copy(
    update={"hero_select": HEROES}
)


def hero_name(card_id):
    return HERO_NAMES.get(card_id, card_id)


def hero_advice(source="llm", pick=89, reason="0 费技能每回合白送属性", comments=(), elapsed=2.34):
    return HeroAdvice(
        source=source,
        signature=HEROES.signature(),
        pick=pick,
        reason=reason,
        comments=tuple(HeroComment(entity_id=e, text=t) for e, t in comments),
        elapsed=elapsed,
    )


def test_show_hero_rules():
    out = []
    display = AdviceDisplay(hero_name, out.append)
    display.show_hero(HERO_STATE, hero_advice("rules", reason="护甲最少", elapsed=None))
    display.show_hero(HERO_STATE, hero_advice("rules", None, "护甲信息不全，等 AI 建议"))
    assert out == ["  📋 规则：选 凯瑞尔·罗姆（护甲最少）", "  📋 规则：护甲信息不全，等 AI 建议"]


def test_show_hero_ai_with_comments_in_offered_order():
    out = []
    comments = (
        (92, "发现随机，看运气"),
        (90, "要靠对子才强，前期慢"),
        (91, ""),
        (99, "不在备选里"),
    )
    AdviceDisplay(hero_name, out.append).show_hero(HERO_STATE, hero_advice(comments=comments))
    assert out == [
        "  🤖 AI（2.3 秒）：选 凯瑞尔·罗姆 —— 0 费技能每回合白送属性",
        "          泰瑟兰·血望者：要靠对子才强，前期慢",
        "          了不起的杰弗里斯：发现随机，看运气",
    ]


def test_show_hero_ai_agreeing_with_rules():
    out = []
    advice = hero_advice(comments=((89, "稳"),))
    AdviceDisplay(hero_name, out.append).show_hero(HERO_STATE, advice, agrees=True)
    assert out == [
        "  🤖 ✓ AI 也选 凯瑞尔·罗姆（2.3 秒）：0 费技能每回合白送属性",
        "          凯瑞尔·罗姆：稳",  # 推荐的那个有点评也列出
    ]


def test_show_hero_with_an_unknown_entity_uses_its_number():
    out = []
    AdviceDisplay(hero_name, out.append).show_hero(HERO_STATE, hero_advice(pick=7, elapsed=None))
    assert out == ["  🤖 AI（?）：选 #7 —— 0 费技能每回合白送属性"]


def test_display_group_forwards_show_hero():
    first, second = [], []
    group = DisplayGroup(
        AdviceDisplay(hero_name, first.append), AdviceDisplay(hero_name, second.append)
    )
    group.show_hero(HERO_STATE, hero_advice("rules", reason="护甲最少"))
    assert first == second == ["  📋 规则：选 凯瑞尔·罗姆（护甲最少）"]


def test_rules_error_at_hero_select_says_so():
    out = []
    display = AdviceDisplay(name, out.append)
    display.rules_error()
    display.rules_error(hero=True)
    assert out == ["  📋 本回合规则建议出错", "  📋 选英雄规则建议出错"]


def choice_advice(pick=6933, reason="32/32 直接成主力", elapsed=1.43, choice_id=12):
    from lscs.advisor.models import ChoiceAdvice

    return ChoiceAdvice(choice_id=choice_id, pick=pick, reason=reason, elapsed=elapsed)


CHOICE_NAMES = {"BG36_356": "泰瑞尔", "BG36_312": "摧心招募者"}


def test_show_choice_prints_only_the_ai_pick():
    from factory import choice_state

    out = []
    display = AdviceDisplay(lambda c: CHOICE_NAMES.get(c, c), out.append)
    state = choice_state()
    display.show_choice(state, None)  # 选项那一行由时间线打印
    display.show_choice(state, choice_advice())
    display.show_choice(state, choice_advice(pick=6931, reason="便宜", elapsed=None))
    display.show_choice(state, choice_advice(pick=99))
    assert out == [
        "  🤖 AI（1.4 秒）：选 左数第 3 个 泰瑞尔 —— 32/32 直接成主力",
        "  🤖 AI（?）：选 左数第 1 个 摧心招募者 —— 便宜",
        "  🤖 AI（1.4 秒）：选 #99 —— 32/32 直接成主力",
    ]


def test_choice_lane_pending_and_errors_use_the_robot_icon():
    out = []
    display = AdviceDisplay(name, out.append)
    display.ai_pending("choice")
    display.ai_text("AI 超时", "choice")
    display.ai_text("AI 建议无效，已忽略", "choice")
    assert out == ["  🤖 AI 分析中…", "  🤖 AI 超时", "  🤖 AI 建议无效，已忽略"]


def test_display_group_forwards_show_choice():
    from factory import choice_state

    first, second = Recorder(), Recorder()
    state, advice = choice_state(), choice_advice()
    DisplayGroup(first, second).show_choice(state, advice)
    assert first.calls == second.calls == [("show_choice", (state, advice))]
