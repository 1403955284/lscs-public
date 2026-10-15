from factory import hand_spell, make_state, minion, power, shop_minion, shop_spell
from helpers import NEEDS_STATS_FIXTURES

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
    group.ai_repairing("fast", "第 2 步：铸币不够")
    group.show_ai(STATE, advice, True)
    group.show_plan(STATE, GAME_PLAN, None)
    group.ai_summary(2, 300, 40)
    group.ai_summary(2, 300, 40, 3)
    expected = [
        ("show_rules", (STATE, advice)),
        ("rules_error", (False,)),
        ("rules_error", (True,)),
        ("ai_pending", ("fast",)),
        ("ai_pending", ("deep",)),
        ("ai_text", ("AI 超时", "fast")),
        ("ai_text", ("深度分析超时", "deep")),
        ("ai_repairing", ("fast", "第 2 步：铸币不够")),
        ("show_ai", (STATE, advice, True)),
        ("show_plan", (STATE, GAME_PLAN, None)),
        ("ai_summary", (2, 300, 40, 0)),
        ("ai_summary", (2, 300, 40, 3)),
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


def test_describe_action_short_for_the_panel():
    # 悬浮面板的"然后"和"规则另议"用短写：不带身材、"使用英雄技能"写成"用技能"
    state = STATE.model_copy(
        update={
            "me": STATE.me.model_copy(
                update={"hero_power": power("AIM", targets=(10,)), "tavern_tier": 3}
            )
        }
    )

    def short(action):
        return describe_action(action, state, name, reasons=False, short=True)

    assert short(Action(type="buy", entity_id=20, reason="好")) == "买 海盗"
    assert short(Action(type="buy", entity_id=30)) == "买 香蕉（法术）"
    assert short(Action(type="buy", entity_id=99)) == "买 #99"
    assert short(Action(type="sell", entity_id=10)) == "卖 小鬼"
    assert short(Action(type="sell", entity_id=99)) == "卖 #99"
    assert short(Action(type="upgrade")) == "升本 → 4 本"
    assert short(Action(type="hero_power", entity_id=10)) == "用技能 瞄准 → 小鬼"
    assert short(Action(type="hero_power", entity_id=99)) == "用技能 瞄准 → #99"
    assert short(Action(type="refresh")) == "刷新"
    assert short(Action(type="freeze")) == "冻结"
    plain = make_state(hero_power_state=power())
    assert describe_action(Action(type="hero_power"), plain, name, short=True) == "用技能 定罪"
    assert describe_action(Action(type="hero_power"), make_state(), name, short=True) == "用技能"
    # 理由照常跟在后面（面板不用，但参数是独立的）
    assert describe_action(Action(type="refresh", reason="找牌"), state, name, short=True) == (
        "刷新（找牌）"
    )
    # short=False 和原来逐字相同
    assert describe_action(Action(type="buy", entity_id=30), state, name) == "买 香蕉（法术，1 金）"
    assert describe_action(Action(type="hero_power", entity_id=10), state, name) == (
        "使用英雄技能 瞄准 → 小鬼 1/1"
    )


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


def test_repairing_lines_say_why_and_what_happens_next():
    out = []
    display = AdviceDisplay(name, out.append)
    display.ai_repairing("fast", "第 2 步：铸币不够")
    display.ai_repairing("deep", "第 3 步：刷新要放在最后，刷新之后不能再有动作")
    display.ai_repairing("hero", "推荐的英雄不在备选里")
    display.ai_repairing("choice", "推荐的选项不在选项里")
    assert out == [
        "  🤖 AI 建议不合法（第 2 步：铸币不够），正在让它改正…",
        "  🧠 深度分析的操作不合法（第 3 步：刷新要放在最后，刷新之后不能再有动作），正在让它改正…",
        "  🤖 AI 建议不合法（推荐的英雄不在备选里），正在让它改正…",
        "  🤖 AI 建议不合法（推荐的选项不在选项里），正在让它改正…",
    ]


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


def pool_note(card_id, card_name, have, left, shop=0):
    from lscs.advisor.pool import PoolNote

    return PoolNote(card_id, card_name, have, left, shop)


PAIR_NOTES = (
    pool_note("BG36_099", "蚀脑怪", 2, 11),
    pool_note("BG36_116", "孢林卵怪", 1, 14, shop=1),
    pool_note("BG36_113", "飘荡祭品", 0, 11, shop=1),  # 只是要找的牌：不写进对子行
)


def test_show_rules_lists_pairs_with_pool_counts():
    out = []
    display = AdviceDisplay(name, out.append)
    plan = Plan(source="rules", actions=(Action(type="buy", entity_id=20, reason="好"),))
    display.show_rules(STATE, Advice(state_seq=1, turn=3, plan=plan, pool_notes=PAIR_NOTES))
    assert out == [
        "  📋 规则：① 买 海盗 3/2（好）",
        "          对子：蚀脑怪 ×2（池里还剩约 11 张）；孢林卵怪 ×1 商店有（还剩约 14 张）",
    ]
    # 方案不变、对子变了（买了一张）：再打印一次
    fewer = (pool_note("BG36_099", "蚀脑怪", 3, 10),)
    display.show_rules(STATE, Advice(state_seq=2, turn=3, plan=plan, pool_notes=fewer))
    display.show_rules(STATE, Advice(state_seq=3, turn=3, plan=plan, pool_notes=fewer))
    assert out[2:] == [
        "  📋 规则：① 买 海盗 3/2（好）",
        "          对子：蚀脑怪 ×3（池里还剩约 10 张）",
    ]


def test_show_rules_without_pairs_has_no_pair_line():
    out = []
    plan = Plan(source="rules", actions=(Action(type="refresh"),))
    notes = (PAIR_NOTES[2],)
    AdviceDisplay(name, out.append).show_rules(
        STATE, Advice(state_seq=1, turn=3, plan=plan, pool_notes=notes)
    )
    assert out == ["  📋 规则：① 刷新"]


def test_look_for_shows_pool_counts():
    out = []
    plan = Plan(
        source="llm",
        actions=(Action(type="refresh"),),
        direction="畸变",
        look_for=("飘荡祭品", "深渊使者", "蚀脑怪(金)"),
    )
    advice = Advice(state_seq=1, turn=3, plan=plan, elapsed=1.0, pool_notes=PAIR_NOTES)
    AdviceDisplay(name, out.append).show_ai(STATE, advice, False)
    assert out[-1] == (
        "          方向：畸变   要找：飘荡祭品（还剩约 11 张）、深渊使者、蚀脑怪(金)（还剩约 11 张）"
    )
    out.clear()
    AdviceDisplay(name, out.append).show_ai(STATE, advice, True)  # 同意规则时也写
    assert out[-1].endswith("要找：飘荡祭品（还剩约 11 张）、深渊使者、蚀脑怪(金)（还剩约 11 张）")


OPP_NAMES = {"H2": "洛卡拉", "H3": "厄祖玛特", "H4": "布莱恩", "H5": "詹迪斯", "H6": "范克里夫"}
OPP_NAMES.update({"P": "夺金健将", "W": "飘荡祭品", "E": "电音合成师", "B": "蚀脑怪"})


def opp_name(card_id):
    return OPP_NAMES.get(card_id, card_id)


def opponents_state(next_id=5, turn=10):
    from lscs.state.models import OpponentView

    def opp(player_id, health, armor, place, tier, board=(), seen=None):
        return OpponentView(
            player_id=player_id,
            hero_card_id=f"H{player_id}",
            health=health,
            armor=armor,
            place=place,
            tavern_tier=tier,
            last_board=tuple(board),
            last_seen_turn=seen,
        )

    aberrations = [minion(i, "W", 10, 10, tier=3, races=["ABERRATION"]) for i in (1, 2)]
    ghost = [minion(5, "P", 7, 7, golden=True, races=["PIRATE"]), minion(6, "E", 3, 5, tier=2)]
    opponents = {
        2: opp(2, 30, 9, 1, 5, aberrations, 9),
        3: opp(3, 30, 3, 3, 5, [minion(3, "B", 90, 90, tier=2, races=["ABERRATION"])], 8),
        4: opp(4, 28, 0, None, None),
        5: opp(5, 0, 0, 8, 2, ghost, 4),
        6: opp(6, -9, 0, 7, 5, [minion(7, "B", 3, 4)], 5),
    }
    return make_state(turn=turn, tribes={"ABERRATION", "PIRATE"}).model_copy(
        update={"opponents": opponents, "next_opponent_id": next_id}
    )


def test_show_opponents_block():
    from lscs.advisor.opponents import summarize_opponents

    out = []
    state = opponents_state()
    AdviceDisplay(opp_name, out.append).show_opponents(state, summarize_opponents(state))
    assert out == [
        "  👥 对手（主打：畸变 2 家、未知 1）",
        "     第 1 名 洛卡拉 30+9 · 5 本 · 畸变×2 身材 40 · 飘荡祭品、飘荡祭品 · 第 9 回合见 · 威胁 48",
        "   ★ 第 3 名 厄祖玛特 30+3 · 5 本 · 畸变×1 身材 180 · 蚀脑怪 · 第 8 回合见 · 威胁 93",
        "     第 ? 名 布莱恩 28+0 · ? 本 · 还没见过阵容 · 威胁 14",
        # 下一个对手是已淘汰的玩家：打它的残影，阵容全列出来
        "   ▶ 第 8 名 詹迪斯 已淘汰（残影）· 第 4 回合见到的阵容：夺金健将(金)、电音合成师",
        "     已淘汰：范克里夫",  # 其他淘汰的合成一行
    ]


def test_show_opponents_marks_next_and_strongest_together():
    from lscs.advisor.opponents import summarize_opponents

    out = []
    state = opponents_state(next_id=3)
    AdviceDisplay(opp_name, out.append).show_opponents(state, summarize_opponents(state))
    assert out[2].startswith("  ▶★ 第 3 名 厄祖玛特")
    assert out[4:] == ["     已淘汰：范克里夫、詹迪斯"]  # 按名次


def test_display_group_forwards_show_opponents():
    from lscs.advisor.opponents import summarize_opponents

    first, second = Recorder(), Recorder()
    state = opponents_state()
    view = summarize_opponents(state)
    DisplayGroup(first, second).show_opponents(state, view)
    assert first.calls == second.calls == [("show_opponents", (state, view))]


def test_empty_captured_board_is_written_as_such():
    from lscs.advisor.opponents import summarize_opponents
    from lscs.state.models import OpponentView

    empty = OpponentView(
        player_id=7,
        hero_card_id="H7",
        health=20,
        armor=0,
        place=2,
        tavern_tier=3,
        last_board=(),
        last_seen_turn=5,
    )
    state = opponents_state().model_copy(update={"opponents": {7: empty}, "next_opponent_id": 7})
    out = []
    AdviceDisplay(opp_name, out.append).show_opponents(state, summarize_opponents(state))
    assert out[1] == "  ▶★ 第 2 名 H7 20+0 · 3 本 · 第 5 回合见到空场 · 威胁 35"


def _review():
    from datetime import datetime

    from test_review_flags import RULES, curve_turns, record
    from test_review_flags import name as card_name

    from lscs.review.report import review_game

    turns = curve_turns({5: dict(tier_start=3, tier_end=4, buys=())})
    return review_game(record(turns), RULES, card_name, datetime(2026, 10, 11, 23, 20))


def test_show_review_prints_the_rules_block():
    from lscs.review.report import terminal_lines

    out = []
    review = _review()
    AdviceDisplay(name, out.append).show_review(STATE, review)
    assert out == terminal_lines(review)
    assert out[0] == "  📝 复盘：H 第 4 名（10 回合）"


def test_display_group_forwards_show_review():
    seen = []

    class Panel:
        def show_review(self, state, review):
            seen.append(review)

    class Broken:
        def show_review(self, state, review):
            raise RuntimeError("坏了")

    review = _review()
    DisplayGroup(Broken(), Panel()).show_review(STATE, review)
    assert seen == [review]


# ---- 站位（站位建议设计文档第 6.1 节） ----

from lscs.advisor.models import Move, PositionAdvice  # noqa: E402

POS_NAMES = {"C": "蚀脑怪", "D": "虚悯者", "SP": "孢林卵怪", "SK": "无害的骨颅", "IR": "铁血防御机"}
DEATHRATTLE = "亡语随从先出手、先倒下，亡语最早生效，后面的随从都能用上"
AVENGE = "复仇要等友方先倒下几个才触发，放右边活到那时候"


def pos_name(card_id):
    return POS_NAMES.get(card_id, card_id)


def pos_state(*cards, turn=3):
    return make_state(turn=turn, board=[minion(i, c, 2, 2) for i, c in enumerate(cards, start=1)])


def pos_move(entity_id, index, before_id=None, role="deathrattle", reason=DEATHRATTLE):
    return Move(
        entity_id=entity_id, index=index, before_id=before_id, role=role, reason=reason, short=""
    )


def position(board, moves=(), turn=3, intro=False, more=0):
    return PositionAdvice(
        state_seq=1,
        turn=turn,
        board=tuple(board),
        target=tuple(board),
        moves=tuple(moves),
        more=more,
        intro=intro,
    )


def test_position_step_intro_once_then_done():
    out = []
    display = AdviceDisplay(pos_name, out.append)
    bought = pos_state("C", "D", "SP")
    display.show_position(bought, None)  # 还没买完
    display.show_position(bought, position((1, 2, 3), [pos_move(3, 0)], intro=True))
    display.show_position(bought, position((1, 2, 3), [pos_move(3, 0)]))  # 铸币变了，同一步
    dragged = pos_state("SP", "C", "D")
    display.show_position(dragged, position((3, 1, 2)))
    display.show_position(dragged, position((3, 1, 2)))
    assert out == [
        "  🧭 站位规则：战斗时随从从最左边开始依次出手；亡语召唤的随从出现在原位；"
        "嘲讽会被优先攻击。本局只提示这一次",
        f"  🧭 站位：把 左数第 3 个 孢林卵怪 拖到最左边（{DEATHRATTLE}）",
        "  🧭 站位已调好",
    ]


def test_position_two_steps_are_numbered_and_more_says_do_these_first():
    out = []
    state = pos_state("C", "IR", "D", "SK", "C")
    moves = [pos_move(4, 0), pos_move(2, 4, role="avenge", reason=AVENGE)]
    AdviceDisplay(pos_name, out.append).show_position(state, position((1, 2, 3, 4, 5), moves))
    assert out == [
        f"  🧭 站位：① 把 左数第 4 个 无害的骨颅 拖到最左边（{DEATHRATTLE}）",
        f"          ② 把 左数第 3 个 铁血防御机 拖到最右边（{AVENGE}）",
    ]
    out.clear()
    state = pos_state("C", "D", "SP", "SK", "SP")
    moves = [pos_move(3, 0), pos_move(4, 1, before_id=1)]
    AdviceDisplay(pos_name, out.append).show_position(
        state, position((1, 2, 3, 4, 5), moves, more=1)
    )
    assert out == [
        f"  🧭 站位：① 把 左数第 3 个 孢林卵怪 拖到最左边（{DEATHRATTLE}）",
        f"          ② 把 左数第 4 个 无害的骨颅 拖到 蚀脑怪 的左边（{DEATHRATTLE}）；先做这两步",
    ]


def test_position_done_only_when_the_same_minions_are_on_the_board():
    out = []
    display = AdviceDisplay(pos_name, out.append)
    display.show_position(pos_state("C", "D", "SP"), position((1, 2, 3), [pos_move(3, 0)]))
    # 卖掉了孢林卵怪：不是"调好了"
    display.show_position(pos_state("C", "D"), position((1, 2)))
    # 买了一个新的亡语随从：打新的一步
    state = make_state(
        turn=3, board=[minion(1, "C", 2, 2), minion(2, "D", 2, 2), minion(4, "SK", 1, 1)]
    )
    display.show_position(state, position((1, 2, 4), [pos_move(4, 0)]))
    assert out == [
        f"  🧭 站位：把 左数第 3 个 孢林卵怪 拖到最左边（{DEATHRATTLE}）",
        f"  🧭 站位：把 左数第 3 个 无害的骨颅 拖到最左边（{DEATHRATTLE}）",
    ]


def test_position_each_turn_once_and_nothing_without_steps():
    out = []
    display = AdviceDisplay(pos_name, out.append)
    display.show_position(pos_state("C", "D"), position((1, 2)))  # 没有要动的
    state = pos_state("D", "SP", turn=9)
    for turn in (9, 9, 10):
        display.show_position(state, position((1, 2), [pos_move(2, 0)], turn=turn))
    line = f"  🧭 站位：把 左数第 2 个 孢林卵怪 拖到最左边（{DEATHRATTLE}）"
    assert out == [line, line]


def test_display_group_forwards_show_position():
    seen = []

    class Panel:
        def show_position(self, state, advice):
            seen.append(advice)

    class Broken:
        def show_position(self, state, advice):
            raise RuntimeError("坏了")

    advice = position((1, 2))
    DisplayGroup(Broken(), Panel()).show_position(STATE, advice)
    DisplayGroup(Panel()).show_position(STATE, None)
    assert seen == [advice, None]


def test_position_memory_resets_for_a_new_game():
    out = []
    display = AdviceDisplay(pos_name, out.append)
    state = pos_state("D", "SP", turn=9)
    step = [pos_move(2, 0)]
    line = f"  🧭 站位：把 左数第 2 个 孢林卵怪 拖到最左边（{DEATHRATTLE}）"
    display.show_position(state, position((1, 2), step, turn=9))
    # 下一局（回合数变小了）：同一回合、同样的步还要再打（实体编号在合成局面里正好相同）
    display.show_position(pos_state("D", "SP", turn=3), position((1, 2), turn=3))
    display.show_position(state, position((1, 2), step, turn=9))
    # 下一局的第一条站位建议带着机制说明：即使回合和步都和上一局最后一条一样，也照常打
    display.show_position(state, position((1, 2), step, turn=9, intro=True))
    assert out == [line, line, out[2], line]
    assert out[2].startswith("  🧭 站位规则：")
    # 回合数变小时不会把上一局的步当成"已调好"
    out.clear()
    display.show_position(pos_state("D", "SP", turn=2), position((1, 2), turn=2))
    assert out == []


# ---- 本版本统计（本版本统计设计文档第 6.3、7.1 节） ----

STAT_NAMES = {
    "TB_BaconShop_HERO_91": "了不起的杰弗里斯",
    "BG21_HERO_000": "凯瑞尔·罗姆",
    "BG28_HERO_800": "泰瑟兰·血望者",
    "BG33_HERO_001": "洛，在世传奇",
    "BG99_HERO_LOW": "样本少的英雄",
    "BG35_MagicItem_890": "沉没的船锚",
    "BG36_MagicItem_402": "精神鞭笞者肖像",
    "BG30_MagicItem_703": "神秘魔方",
    "BG30_MagicItem_420": "麦迪文之书",
}


def stat_name(card_id):
    return STAT_NAMES.get(card_id, card_id)


def stat_heroes(heroes):
    select = HeroSelect(
        offered=tuple(
            HeroOption(entity_id=e, card_id=c, position=i, armor=a, health=30, hero_power_dbf=None)
            for i, (e, c, a) in enumerate(heroes, start=1)
        )
    )
    state = make_state(turn=0, phase=Phase.HERO_SELECT, hero="TB_BaconShop_HERO_PH")
    return state.model_copy(update={"hero_select": select})


USER_HEROES = (
    (92, "TB_BaconShop_HERO_91", 17),
    (89, "BG21_HERO_000", 10),
    (90, "BG28_HERO_800", 18),
    (91, "BG33_HERO_001", 19),
)
STATS_SOURCE = "数据来自 Firestone · 本补丁 · 全部玩家 · 10-14 更新"


@NEEDS_STATS_FIXTURES
def test_show_stats_for_heroes_sorted_best_first():
    from helpers import fixture_stats

    from lscs.advisor.stats_note import hero_note

    state = stat_heroes(USER_HEROES)
    out = []
    AdviceDisplay(stat_name, out.append).show_stats(
        state, hero_note(state.hero_select, fixture_stats())
    )
    assert out == [
        f"  📊 本版本战绩（{STATS_SOURCE}）：",
        "          ▲ 凯瑞尔·罗姆 第 3.67 名 · 选 28% · 第 2/10",
        "            洛，在世传奇 第 3.91 名 · 选 29% · 第 4/10",
        "            泰瑟兰·血望者 第 3.99 名 · 选 15% · 第 5/10",
        "            了不起的杰弗里斯 第 4.04 名 · 选 27% · 第 7/10",
    ]


@NEEDS_STATS_FIXTURES
def test_show_stats_low_sample_and_missing_heroes():
    from helpers import fixture_stats

    from lscs.advisor.stats_note import hero_note

    heroes = ((1, "BG99_HERO_LOW", 15), (2, "UNKNOWN_HERO", 12), (3, "BG28_HERO_800", 18))
    state = stat_heroes(heroes)
    note = hero_note(state.hero_select, fixture_stats())
    assert note.best == 3 and [r.entity_id for r in note.rows] == [1, 3]
    out = []
    AdviceDisplay(stat_name, out.append).show_stats(state, note)
    # 查不到的英雄不列；样本少的照样列，不标 ▲
    assert out[1:] == [
        "          ▲ 泰瑟兰·血望者 第 3.99 名 · 选 15% · 第 5/10",
        "            样本少的英雄 第 4.20 名（样本少）",
    ]


def trinket_choice():
    from factory import choice_option, make_choice

    trinkets = (
        (3027, "BG35_MagicItem_890", 4),
        (3026, "BG36_MagicItem_402", 4),
        (3025, "BG30_MagicItem_703", 5),
        (3028, "BG30_MagicItem_420", 1),
    )
    options = [
        choice_option(e, c, i, card_type="BATTLEGROUND_TRINKET", cost=cost)
        for i, (e, c, cost) in enumerate(trinkets)
    ]
    return make_choice(options, choice_id=6, kind="trinket", source="BG30_Trinket_1st")


@NEEDS_STATS_FIXTURES
def test_show_stats_for_trinkets_with_their_price():
    from factory import choice_state
    from helpers import fixture_stats

    from lscs.advisor.stats_note import trinket_note

    choice = trinket_choice()
    state = choice_state(choice, turn=6)
    out = []
    AdviceDisplay(stat_name, out.append).show_stats(state, trinket_note(choice, fixture_stats()))
    # 平均名次相同（3.95）时按画面顺序
    assert out == [
        f"  📊 饰品本版本战绩（{STATS_SOURCE}）：",
        "          ▲ 精神鞭笞者肖像（4 金）第 3.89 名 · 选 27%",
        "            神秘魔方（5 金）第 3.95 名 · 选 30%",
        "            麦迪文之书（1 金）第 3.95 名 · 选 18%",
        "            沉没的船锚（4 金）第 4.06 名 · 选 15%",
    ]


@NEEDS_STATS_FIXTURES
def test_display_group_forwards_show_stats_and_the_review_tap_ignores_it():
    from factory import choice_state
    from helpers import fixture_stats

    from lscs.advisor.stats_note import trinket_note
    from lscs.review.reviewer import AdviceCollector

    seen = []

    class Panel:
        def show_stats(self, state, note):
            seen.append(note)

    class Broken:
        def show_stats(self, state, note):
            raise RuntimeError("坏了")

    choice = trinket_choice()
    note = trinket_note(choice, fixture_stats())
    collector = AdviceCollector()
    DisplayGroup(Broken(), collector, Panel()).show_stats(choice_state(choice), note)
    assert seen == [note]
    assert collector.logs == {} and collector.ai_choices == {}


# ---- 整局计划（整局计划与流派指南设计文档第 8.1 节） ----

PLAN_NAMES = {"BG36_099": "蚀脑怪", "BG36_099_G": "蚀脑怪(金)", "BG36_113": "飘荡祭品"}


def plan_name(card_id):
    return PLAN_NAMES.get(card_id, card_id)


def plan_of(**fields):
    from lscs.advisor.models import GamePlan

    values = {
        "comp": "畸变神明",
        "tribe": "ABERRATION",
        "key_cards": ("蚀脑怪", "残忍的精神鞭笞者", "飘荡祭品"),
        "tiers": "第 6 回合 4 本，第 8 回合 5 本",
        "roll": "5 本后铸币主要刷新找核心卡；血量低于 15 先补身材",
        "stage": "过渡",
        "pivot": "第 8 回合还没有两张核心卡就转海盗",
        "turn": 5,
    }
    values.update(fields)
    return GamePlan(**values)


GAME_PLAN = plan_of()
PLAN_STATE = make_state(turn=5, board=[minion(10, "BG36_099_G", 6, 8, races=["ABERRATION"])])


def plan_output(plan, previous=None, state=PLAN_STATE):
    out = []
    AdviceDisplay(plan_name, out.append).show_plan(state, plan, previous)
    return out


def test_new_plan_prints_two_lines_with_the_held_key_cards_marked():
    assert plan_output(GAME_PLAN) == [
        "  🗺 计划：畸变神明 · 核心 蚀脑怪✓、残忍的精神鞭笞者、飘荡祭品 · 第 6 回合 4 本，"
        "第 8 回合 5 本 · 阶段 过渡",
        "          规则：5 本后铸币主要刷新找核心卡；血量低于 15 先补身材   "
        "换方向：第 8 回合还没有两张核心卡就转海盗",
    ]


def test_new_plan_with_few_fields():
    plan = plan_of(comp="刀剑流", tribe="PIRATE", key_cards=(), tiers="", roll="", pivot="")
    assert plan_output(plan) == ["  🗺 计划：刀剑流（海盗） · 阶段 过渡"]
    named = plan.model_copy(update={"comp": "海盗刀剑", "stage": None})
    assert plan_output(named) == ["  🗺 计划：海盗刀剑"]  # 流派名里有种族名：不重复写
    undecided = plan_of(comp="未定", key_cards=("飘荡祭品",), stage="前期", roll="", pivot="")
    assert plan_output(undecided) == [
        "  🗺 计划：未定 · 想找 飘荡祭品 · 第 6 回合 4 本，第 8 回合 5 本 · 阶段 前期"
    ]


def test_changed_plan_gives_the_reason_and_only_what_changed():
    new = plan_of(
        key_cards=("蚀脑怪", "飘荡祭品"),
        stage="保血",
        roll="血量低于 15 先补身材",
        changed="连输两场掉到 19 血，先补身材",
        turn=9,
    )
    assert plan_output(new, GAME_PLAN) == [
        "  🗺 计划改了：畸变神明 · 核心 蚀脑怪✓、飘荡祭品 · 阶段 保血",
        "          原因：连输两场掉到 19 血，先补身材",
        "          规则：血量低于 15 先补身材",
    ]
    silent = plan_of(comp="刀剑流", tribe="PIRATE", key_cards=(), turn=9)
    assert plan_output(silent, GAME_PLAN) == ["  🗺 计划改了：刀剑流（海盗） · 阶段 过渡"]


def test_a_new_stage_alone_prints_one_line_and_the_reason():
    stage = plan_of(stage="保血", changed="血量 13", turn=9)
    assert plan_output(stage, GAME_PLAN) == [
        "  🗺 阶段：保血（原来 过渡）",
        "          原因：血量 13",
    ]
    assert plan_output(plan_of(stage="成型"), GAME_PLAN) == ["  🗺 阶段：成型（原来 过渡）"]


def test_plan_note_goes_at_the_end_of_the_ai_block():
    plan = Plan(source="llm", actions=(Action(type="refresh"),), summary="刷")
    note = "商店里有核心卡 海盗，方案却没买它"
    for agrees, first in ((False, "  🤖 AI（1.0 秒）：① 刷新"), (True, "  🤖 ✓ AI 同意规则建议")):
        out = []
        advice = Advice(state_seq=1, turn=3, plan=plan, elapsed=1.0, plan_note=note)
        AdviceDisplay(name, out.append).show_ai(STATE, advice, agrees)
        assert out[0].startswith(first)
        assert out[-1] == f"          ⚠ 计划提醒：{note}"
    out = []
    advice = Advice(state_seq=1, turn=3, plan=plan, elapsed=1.0)
    AdviceDisplay(name, out.append).show_ai(STATE, advice, False)
    assert not any("计划提醒" in line for line in out)


def test_ai_summary_counts_the_fast_advice_off_the_plan():
    out = []
    display = AdviceDisplay(name, out.append)
    display.ai_summary(79, 29300, 11200, 3)
    display.ai_summary(79, 29300, 11200)
    assert out == [
        "本局 AI 调用 79 次，输入 29.3 千、输出 11.2 千 token，快速建议偏离计划 3 次",
        "本局 AI 调用 79 次，输入 29.3 千、输出 11.2 千 token",
    ]


def test_choosing_a_comp_after_undecided_reads_like_a_new_plan():
    undecided = plan_of(comp="未定", key_cards=("飘荡祭品",), stage="前期", roll="", pivot="")
    assert plan_output(GAME_PLAN, undecided)[0].startswith("  🗺 计划：畸变神明 · 核心 ")
    other = undecided.model_copy(update={"key_cards": ("蚀脑怪",), "stage": "过渡"})
    assert plan_output(other, undecided) == ["  🗺 阶段：过渡（原来 前期）"]


def test_rewording_alone_is_not_a_changed_plan_in_the_terminal():
    reworded = plan_of(key_cards=("飘荡祭品", "残忍的精神鞭笞者", "蚀脑怪"), stage="成型")
    assert plan_output(reworded, GAME_PLAN) == ["  🗺 阶段：成型（原来 过渡）"]
