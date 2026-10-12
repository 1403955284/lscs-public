import threading

from factory import hero_select_state, make_state, minion, offered_event, power, shop_minion

from lscs.advisor.models import Action, Advice, HeroAdvice, HeroComment, Plan
from lscs.llm.notices import AUTH_DISABLED, BUDGET_EXHAUSTED
from lscs.overlay.model import IDLE, Line, NextStep, OverlayModel, OverlayView, Target, condense
from lscs.state.models import Event, EventType, Phase

NAMES = {
    "A": "小鬼",
    "B": "海盗",
    "C": "蚀脑怪",
    "BG21_HERO_000p": "定罪",
    "BG21_HERO_000": "凯瑞尔·罗姆",
    "BG28_HERO_800": "泰瑟兰·血望者",
    "BG33_HERO_001": "洛，在世传奇",
    "TB_BaconShop_HERO_91": "了不起的杰弗里斯",
    "BG22_HERO_003": "范达尔·雷矛",
}


def name(card_id):
    return NAMES.get(card_id, card_id)


SHOP = [shop_minion(20, "B", 3, 2), shop_minion(21, "C", 3, 4)]
T3 = make_state(turn=3, gold=5, tier=2, upgrade_cost=6, shop=SHOP, board=[minion(10, "A", 1, 1)])


def started(turn):
    return [Event(type=EventType.RECRUIT_STARTED, turn=turn)]


def rules_advice(turn=3, direction="海盗"):
    plan = Plan(
        source="rules",
        actions=(Action(type="buy", entity_id=20, reason="好"), Action(type="refresh")),
        summary="买最好的身材",
        direction=direction,
    )
    return Advice(state_seq=1, turn=turn, plan=plan)


def ai_advice(turn=3, summary="", stale=None, actions=None):
    plan = Plan(
        source="llm",
        actions=actions or (Action(type="buy", entity_id=21, reason="身材好"),),
        summary=summary,
    )
    return Advice(state_seq=1, turn=turn, plan=plan, stale_note=stale, elapsed=2.0)


def test_starts_idle():
    view = OverlayModel(name).snapshot()
    assert view.title == IDLE == "lscs 已就绪，等待招募阶段"
    assert view.compact


def test_a_whole_turn():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    view = model.snapshot()
    assert view.title == "第 3 回合 · 铸币 5 · 2 本 · 升本 6"
    assert not view.compact
    assert (view.rules, view.ai, view.warning) == ((), (), None)

    model.show_rules(T3, rules_advice())
    assert model.snapshot().rules == (
        Line("① 买 海盗 3/2  ② 刷新"),
        Line("方向：海盗", role="direction"),
    )

    model.ai_pending()
    assert model.snapshot().ai == (Line("分析中…", "muted"),)

    model.show_ai(T3, ai_advice(summary="先保血"), agrees=False)
    assert model.snapshot().ai == (Line("① 买 蚀脑怪 3/4"), Line("目标：先保血", role="reason"))

    combat = make_state(turn=3, phase=Phase.COMBAT, seq=2)
    model.on_update(combat, [Event(type=EventType.COMBAT_STARTED, turn=3)])
    view = model.snapshot()
    assert (view.title, view.compact) == ("第 3 回合 · 战斗中", True)

    model.on_update(make_state(turn=4, gold=6, seq=3), started(4))
    view = model.snapshot()
    assert view.title.startswith("第 4 回合 · 铸币 6")
    assert (view.rules, view.ai) == ((), ())  # 新回合清空上一回合的建议

    over = make_state(turn=4, phase=Phase.GAME_OVER, seq=4).model_copy(update={"final_place": 3})
    model.on_update(over, [Event(type=EventType.GAME_ENDED, turn=4, data={"place": 3})])
    view = model.snapshot()
    assert (view.title, view.compact) == ("对局结束 · 第 3 名", True)


def test_advice_shown_before_the_state_update_of_the_same_turn_is_kept():
    model = OverlayModel(name)
    model.on_update(make_state(turn=2, seq=1), started(2))
    model.show_rules(T3, rules_advice(turn=3))  # 建议先到，状态更新后到
    model.on_update(T3, started(3))
    assert model.snapshot().rules[0] == Line("① 买 海盗 3/2  ② 刷新")


def test_agreeing_ai():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(), agrees=True)
    assert model.snapshot().ai == (Line("✓ 同意规则建议"),)


def test_stale_ai_and_long_summary_and_many_steps():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    steps = tuple(Action(type="refresh") for _ in range(6))
    summary = "先" * 43
    model.show_ai(T3, ai_advice(summary=summary, stale="基于 6 秒前的局面", actions=steps), False)
    ai = model.snapshot().ai
    assert ai == (
        Line("① 刷新  ② 刷新  ③ 刷新  ④ 刷新  ⑤ 刷新  …"),  # AI 方案最多 5 步
        Line("目标：" + "先" * 37 + "…", role="reason"),  # 整行最多 40 字，超出加"…"
        Line("基于 6 秒前的局面", "muted", role="note"),
    )
    assert len(ai[1].text) == 41


def test_summary_that_fits_is_not_cut():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(summary="先" * 37), False)
    assert model.snapshot().ai[-1] == Line("目标：" + "先" * 37, role="reason")


BOUGHT = make_state(
    turn=3,
    gold=2,
    tier=2,
    upgrade_cost=6,
    shop=SHOP[1:],
    board=[minion(10, "A", 1, 1), minion(20, "B", 3, 2)],
    seq=2,
)
REFRESHED = BOUGHT.model_copy(
    update={"shop": (shop_minion(30, "A", 1, 1), shop_minion(31, "A", 1, 1)), "seq": 3}
)


def test_rules_steps_already_done_are_dropped():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.on_update(BOUGHT, [])  # 买了建议的海盗；规则这次没有出新建议
    assert model.snapshot().rules == (Line("① 刷新"), Line("方向：海盗", role="direction"))
    model.on_update(REFRESHED, [])  # 商店换了一批：不能说成"已按建议操作完"
    assert model.snapshot().rules == (Line("商店已刷新", "muted"),)


def test_all_steps_done_without_refresh():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    plan = Plan(source="rules", actions=(Action(type="buy", entity_id=20),))
    model.show_rules(T3, Advice(state_seq=1, turn=3, plan=plan))
    model.on_update(BOUGHT, [])  # 商店里还剩 #21，没有刷新
    assert model.snapshot().rules == (Line("已按建议操作完", "muted"),)


def test_rules_after_refresh_keep_steps_that_still_apply():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    plan = Plan(source="rules", actions=(Action(type="upgrade"), Action(type="buy", entity_id=20)))
    model.show_rules(T3, Advice(state_seq=1, turn=3, plan=plan))
    model.on_update(T3.model_copy(update={"shop": REFRESHED.shop, "seq": 2}), [])
    assert model.snapshot().rules == (Line("① 升本 → 3 本"),)  # 等规则的新建议


def test_ai_after_refresh_waits_for_new_advice():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(summary="买蚀脑怪"), False)
    model.on_update(REFRESHED, [])
    assert model.snapshot().ai == (Line("商店已刷新，等待新的 AI 建议", "muted"),)


def test_ai_after_refresh_when_ai_is_off_drops_the_block():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(), False)
    model.ai_text(BUDGET_EXHAUSTED)  # 之后不会再有 AI 建议
    model.show_ai(T3, ai_advice(), False)
    model.on_update(REFRESHED, [])
    assert model.snapshot().ai == (Line("本局 AI 次数已用完", "muted"),)


def test_plan_without_steps_shows_no_action_needed():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    empty = Plan(source="rules", summary="本回合不需要操作", direction="海盗")
    model.show_rules(T3, Advice(state_seq=1, turn=3, plan=empty))
    assert model.snapshot().rules == (Line("无需操作", "muted"),)


def test_ai_steps_already_done_are_dropped():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    actions = (Action(type="buy", entity_id=20), Action(type="buy", entity_id=21))
    model.show_ai(T3, ai_advice(summary="两张都要", actions=actions), False)
    model.on_update(BOUGHT, [])
    assert model.snapshot().ai == (Line("① 买 蚀脑怪 3/4"), Line("目标：两张都要", role="reason"))
    model.on_update(BOUGHT.model_copy(update={"shop": (), "seq": 4}), [])
    assert model.snapshot().ai == (Line("已按建议操作完", "muted"),)


def test_ai_switched_off_mid_game_stays_visible():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.ai_text(BUDGET_EXHAUSTED)
    assert model.snapshot().ai == (Line(BUDGET_EXHAUSTED, "muted"),)
    model.on_update(make_state(turn=4, seq=5), started(4))
    assert model.snapshot().ai == (Line("本局 AI 次数已用完", "muted"),)
    new_game = [Event(type=EventType.GAME_STARTED, turn=0)]
    model.on_update(make_state(turn=0, phase=Phase.HERO_SELECT, seq=6), new_game)
    model.on_update(make_state(turn=1, seq=7), started(1))
    assert model.snapshot().ai == ()  # 预算按局计算，新的一局恢复

    model.ai_text(AUTH_DISABLED)
    model.on_update(make_state(turn=2, seq=8), started(2))
    assert model.snapshot().ai == (Line("AI 已关闭（Key 无效）", "muted"),)
    model.on_update(make_state(turn=0, phase=Phase.HERO_SELECT, seq=9), new_game)
    model.on_update(make_state(turn=1, seq=10), started(1))
    assert model.snapshot().ai == (Line("AI 已关闭（Key 无效）", "muted"),)  # Key 无效一直有效


def test_ai_text_and_rules_error_are_muted():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.rules_error()
    model.ai_text("AI 超时")
    view = model.snapshot()
    assert view.rules == (Line("本回合规则建议出错", "muted"),)
    assert view.ai == (Line("AI 超时", "muted"),)


def test_ai_disabled_and_uncertain_and_full_tier():
    model = OverlayModel(name, ai_enabled=False)
    state = make_state(turn=12, gold=10, tier=6, upgrade_cost=None)  # 满本时没有升本按钮
    model.on_update(state.model_copy(update={"uncertain": True}), started(12))
    view = model.snapshot()
    assert view.title == "第 12 回合 · 铸币 10 · 6 本 · 已满本"
    assert view.ai == (Line("AI 未开启", "muted"),)
    assert view.warning == "⚠ 状态可能不准"


def test_waiting_hero_select_and_new_game():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    # 还没发备选英雄：英雄还是占位的
    hero = make_state(turn=0, phase=Phase.HERO_SELECT, seq=9, hero="TB_BaconShop_HERO_PH")
    model.on_update(hero, [Event(type=EventType.GAME_STARTED, turn=0)])
    assert model.snapshot().title == IDLE
    model.on_update(T3, started(3))  # 新对局的同一个回合数，不能留着上一局的建议
    assert model.snapshot().rules == ()
    over = make_state(phase=Phase.GAME_OVER)
    model.on_update(over, [])
    assert model.snapshot().title == "对局结束"


def test_version_changes_only_when_content_changes():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    first = model.snapshot().version
    model.on_update(T3.model_copy(update={"seq": 5}), [])  # 内容没变
    assert model.snapshot().version == first
    model.show_rules(T3, rules_advice())
    assert model.snapshot().version == first + 1


def test_message_overrides_everything():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_message("监听已停止，请查看终端")
    view = model.snapshot()
    assert (view.title, view.compact) == ("监听已停止，请查看终端", True)


def test_ai_summary_is_accepted():
    OverlayModel(name).ai_summary(3, 1000, 200)  # 面板不显示，但必须有这个方法


def test_concurrent_calls_are_safe():
    model = OverlayModel(name)
    errors = []
    versions = []

    def writer(turn):
        try:
            for i in range(200):
                state = make_state(turn=turn, gold=i % 10, shop=SHOP, seq=i)
                model.on_update(state, started(turn))
                model.show_rules(state, rules_advice(turn=turn))
                model.ai_pending()
                model.show_ai(state, ai_advice(turn=turn), agrees=i % 2 == 0)
        except Exception as exc:  # pragma: no cover - 失败时才走到
            errors.append(exc)

    def reader():
        for _ in range(500):
            versions.append(model.snapshot().version)

    threads = [threading.Thread(target=writer, args=(t,)) for t in (3, 4, 5)]
    threads.append(threading.Thread(target=reader))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert versions == sorted(versions)


def test_condense_drops_direction_first_then_look_for_and_notes():
    view = OverlayView(
        title="第 3 回合",
        compact=False,
        rules=(Line("① 刷新"), Line("方向：海盗", role="direction")),
        ai=(
            Line("① 买 海盗 3/2"),
            Line("方向：纳迦", role="direction"),
            Line("要找：船长", role="look_for"),
            Line("分析：先保血", role="reason"),
            Line("基于 6 秒前的局面", "muted", role="note"),
            Line("深度分析中…", "muted"),
        ),
        ai_title="AI（深度）",
        warning="⚠ 状态可能不准",
    )
    assert condense(view, 0) == view
    one = condense(view, 1)
    assert one.rules == (Line("① 刷新"),)
    assert [line.text for line in one.ai] == [
        "① 买 海盗 3/2",
        "要找：船长",
        "分析：先保血",
        "基于 6 秒前的局面",
        "深度分析中…",
    ]
    two = condense(view, 2)
    assert [line.text for line in two.ai] == ["① 买 海盗 3/2", "分析：先保血", "深度分析中…"]
    assert (two.title, two.warning, two.ai_title) == (view.title, view.warning, view.ai_title)


def test_hero_power_step_shows_and_disappears_once_used():
    model = OverlayModel(name)
    ready = T3.model_copy(update={"me": T3.me.model_copy(update={"hero_power": power()})})
    model.on_update(ready, started(3))
    plan = Plan(
        source="rules",
        actions=(
            Action(type="buy", entity_id=20),
            Action(type="hero_power"),
            Action(type="refresh"),
        ),
    )
    model.show_rules(ready, Advice(state_seq=1, turn=3, plan=plan))
    assert model.snapshot().rules == (Line("① 买 海盗 3/2  ② 使用英雄技能 定罪  ③ 刷新"),)
    used = BOUGHT.model_copy(
        update={"me": BOUGHT.me.model_copy(update={"hero_power": power(used=True)}), "seq": 5}
    )
    model.on_update(used, [])
    assert model.snapshot().rules == (Line("① 刷新"),)


# ---- 快速 + 深度两路 ----


def llm_advice(actions, turn=3, lane="fast", expired=None, state_seq=1, **fields):
    plan = Plan(source="llm", actions=actions, **fields)
    return Advice(
        state_seq=state_seq, turn=turn, plan=plan, elapsed=2.0, lane=lane, expired=expired
    )


BUY_21 = (Action(type="buy", entity_id=21, reason="身材好"),)
BUY_20 = (Action(type="buy", entity_id=20, reason="海盗"),)
DEEP = {
    "analysis": "畸变核心在 5 本，提前升本。",
    "summary": "提前升 5 本找畸变核心",
    "direction": "畸变",
    "look_for": ("神明", "祭品"),
}


def test_fast_ai_lines_show_look_for_and_goal_instead_of_long_analysis():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    advice = llm_advice(
        BUY_21,
        summary="补一张战力，留钱刷新",
        analysis="血量健康，场面偏薄，" * 10,  # 完整分析很长，只在终端里显示
        direction="海盗",
        look_for=("船长",),
    )
    model.show_ai(T3, advice, False)
    view = model.snapshot()
    assert view.ai_title == "AI"
    assert view.ai == (
        Line("① 买 蚀脑怪 3/4"),
        Line("方向：海盗", role="direction"),
        Line("要找：船长", role="look_for"),
        Line("目标：补一张战力，留钱刷新", role="reason"),
    )


def test_analysis_is_shown_only_when_there_is_no_summary():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_21, analysis="血量健康，先补战力。"), False)
    assert model.snapshot().ai[-1] == Line("分析：血量健康，先补战力。", role="reason")


def test_look_for_and_analysis_are_cut():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    names = tuple(f"很长的牌名{i}" for i in range(6))
    model.show_ai(T3, llm_advice(BUY_21, analysis="分" * 50, look_for=names), False)
    look_for, analysis = model.snapshot().ai[1:]
    # 按整张牌截断：放得下的前 5 张，后面加"…"（不会截出半个牌名）
    assert look_for.text == "要找：" + "、".join(names[:5]) + "…"
    assert analysis.text == ("分析：" + "分" * 37) + "…"


def test_deep_analysis_pending_line_and_deep_result_first():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.ai_pending()
    model.ai_pending("deep")
    assert model.snapshot().ai == (Line("分析中…", "muted"), Line("深度分析中…", "muted"))
    model.show_ai(T3, llm_advice(BUY_21, analysis="快速分析"), False)
    assert model.snapshot().ai[-1] == Line("深度分析中…", "muted")
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    view = model.snapshot()
    assert view.ai_title == "AI（深度）"
    assert view.ai == (
        Line("① 买 海盗 3/2"),
        Line("方向：畸变", role="direction"),
        Line("要找：神明、祭品", role="look_for"),
        Line("目标：提前升 5 本找畸变核心", role="reason"),
    )
    model.on_update(make_state(turn=4, seq=9), started(4))
    view = model.snapshot()
    assert (view.ai, view.ai_title) == ((), "AI")  # 新回合清空深度分析


def test_deep_steps_done_keep_the_thoughts():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    model.on_update(BOUGHT, [])
    assert model.snapshot().ai == (
        Line("已按建议操作完", "muted"),
        Line("要找：神明、祭品", role="look_for"),
        Line("目标：提前升 5 本找畸变核心", role="reason"),
    )


def test_expired_deep_result_shows_its_direction_look_for_and_analysis():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    plan = Plan(source="llm", actions=BUY_21, analysis="快速分析", direction="海盗")
    fast = Advice(state_seq=1, turn=3, plan=plan, stale_note="基于 6 秒前的局面", elapsed=2.0)
    model.show_ai(T3, fast, False)
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", expired="invalid", **DEEP), False)
    view = model.snapshot()
    assert view.ai_title == "AI（深度）"
    assert view.ai == (  # 快速建议的操作 + 深度分析的方向和思路 + 快速建议的附注
        Line("① 买 蚀脑怪 3/4"),
        Line("方向：畸变", role="direction"),
        Line("要找：神明、祭品", role="look_for"),
        Line("目标：提前升 5 本找畸变核心", role="reason"),
        Line("基于 6 秒前的局面", "muted", role="note"),
    )
    assert [line.text for line in condense(view, 1).ai][:2] == [
        "① 买 蚀脑怪 3/4",
        "要找：神明、祭品",
    ]


def test_refresh_after_deep_result_keeps_its_thoughts_with_the_new_fast_advice():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_21, analysis="快速分析"), False)
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    model.on_update(REFRESHED, [])  # 深度分析的操作过期
    thoughts = (
        Line("方向：畸变", role="direction"),
        Line("要找：神明、祭品", role="look_for"),
        Line("目标：提前升 5 本找畸变核心", role="reason"),
    )
    assert model.snapshot().ai == (Line("商店已刷新，等待新的 AI 建议", "muted"), *thoughts)
    buy_30 = (Action(type="buy", entity_id=30),)
    model.show_ai(REFRESHED, llm_advice(buy_30, state_seq=3, analysis="刷新后的快速分析"), False)
    view = model.snapshot()
    assert view.ai_title == "AI（深度）"
    assert view.ai == (Line("① 买 小鬼 1/1"), *thoughts)


def test_expired_deep_thoughts_while_the_new_fast_advice_is_pending():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_21, analysis="快速分析"), False)
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    model.on_update(REFRESHED, [])
    model.ai_pending()  # 刷新后的快速建议已经发出
    view = model.snapshot()
    assert view.ai_title == "AI（深度）"
    assert view.ai == (
        Line("分析中…", "muted"),
        Line("方向：畸变", role="direction"),
        Line("要找：神明、祭品", role="look_for"),
        Line("目标：提前升 5 本找畸变核心", role="reason"),
    )


def test_deep_error_only_clears_the_pending_line():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.ai_pending("deep")
    model.show_ai(T3, llm_advice(BUY_21), False)
    model.ai_text("深度分析超时", "deep")
    view = model.snapshot()
    assert (view.ai, view.ai_title) == ((Line("① 买 蚀脑怪 3/4"),), "AI")


def test_deep_agreeing_with_rules_keeps_its_thoughts():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), True)
    view = model.snapshot()
    assert view.ai_title == "AI（深度）"
    assert view.ai == (
        Line("✓ 同意规则建议"),
        Line("要找：神明、祭品", role="look_for"),
        Line("目标：提前升 5 本找畸变核心", role="reason"),
    )


def test_budget_notice_with_deep_pending():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.ai_text(BUDGET_EXHAUSTED)
    model.ai_pending("deep")
    assert model.snapshot().ai == (Line(BUDGET_EXHAUSTED, "muted"), Line("深度分析中…", "muted"))


# ---- 选英雄 ----

HERO_TITLE = "选英雄 · 护甲越少越强"
HERO_ROWS = (
    Line("凯瑞尔·罗姆  护甲 10"),
    Line("泰瑟兰·血望者  护甲 18"),
    Line("洛，在世传奇  护甲 19"),
    Line("了不起的杰弗里斯  护甲 17"),
)
COMMENTS = ((92, "发现随机，看运气"), (90, "要靠对子才强，前期慢"), (91, "技能复杂，新手难发挥"))


def hero_advice(state, source="llm", pick=89, reason="0 费技能每回合白送属性", comments=()):
    return HeroAdvice(
        source=source,
        signature=state.hero_select.signature(),
        pick=pick,
        reason=reason,
        comments=tuple(HeroComment(entity_id=e, text=t) for e, t in comments),
    )


def rules_pick(state, pick=89):
    return hero_advice(state, "rules", pick, "护甲最少")


def choosing(model, state=None):
    state = state or hero_select_state()
    model.on_update(state, [Event(type=EventType.GAME_STARTED, turn=0), offered_event(state)])
    return state


def test_hero_select_view():
    model = OverlayModel(name)
    st = choosing(model)
    view = model.snapshot()
    assert (view.title, view.compact, view.ai_title, view.warning) == (
        HERO_TITLE,
        False,
        "AI",
        None,
    )
    assert (view.rules, view.ai) == (HERO_ROWS, ())  # 规则建议还没到：先列出英雄，不标 ★
    model.show_hero(st, rules_pick(st))
    assert model.snapshot().rules == (Line("★ 凯瑞尔·罗姆  护甲 10"), *HERO_ROWS[1:])
    model.ai_pending("hero")
    assert model.snapshot().ai == (Line("分析中…", "muted"),)
    model.show_hero(st, hero_advice(st, comments=COMMENTS))
    assert model.snapshot().ai == (
        Line("选 凯瑞尔·罗姆：0 费技能每回合白送属性"),
        Line("泰瑟兰·血望者：要靠对子才强，前期慢", role="comment"),  # 按备选顺序
        Line("洛，在世传奇：技能复杂，新手难发挥", role="comment"),
        Line("了不起的杰弗里斯：发现随机，看运气", role="comment"),
    )
    assert model.snapshot().title == HERO_TITLE


def test_hero_rules_without_a_pick_and_ai_agreeing():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, rules_pick(st, pick=None))
    assert (model.snapshot().title, model.snapshot().rules) == (HERO_TITLE, HERO_ROWS)
    model.show_hero(st, hero_advice(st, pick=91, comments=((91, "稳"),)), agrees=True)
    assert model.snapshot().ai == (
        Line("✓ 也选 洛，在世传奇：0 费技能每回合白送属性"),
        Line("洛，在世传奇：稳", role="comment"),
    )


def test_hero_reason_is_cut_to_one_line():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, hero_advice(st, reason="理" * 60))
    first = model.snapshot().ai[0]
    assert first.text == ("选 凯瑞尔·罗姆：" + "理" * 60)[:40] + "…"
    assert first.role == "main"


def test_hero_errors_and_ai_off_are_muted():
    model = OverlayModel(name)
    choosing(model)
    model.ai_text("AI 超时", "hero")
    assert model.snapshot().ai == (Line("AI 超时", "muted"),)
    model.ai_text(BUDGET_EXHAUSTED)  # 调度器的提示不分路：选英雄时显示在选英雄的 AI 区
    assert model.snapshot().ai == (Line(BUDGET_EXHAUSTED, "muted"),)
    model.on_update(make_state(turn=1, seq=5), started(1))
    assert model.snapshot().ai == (Line("本局 AI 次数已用完", "muted"),)
    off = OverlayModel(name, ai_enabled=False)
    choosing(off)
    assert off.snapshot().ai == (Line("AI 未开启", "muted"),)
    disabled = OverlayModel(name)
    disabled.ai_text(AUTH_DISABLED)
    choosing(disabled)  # 新的一局也一直有效
    assert disabled.snapshot().ai == (Line("AI 已关闭（Key 无效）", "muted"),)


def test_condense_drops_hero_comments_at_level_two():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, hero_advice(st, comments=COMMENTS))
    view = model.snapshot()
    assert condense(view, 1) == view
    assert condense(view, 2).ai == (Line("选 凯瑞尔·罗姆：0 费技能每回合白送属性"),)
    assert condense(view, 2).rules == view.rules


def test_reroll_shows_the_new_heroes_and_waits_for_the_ai_again():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, rules_pick(st))
    model.show_hero(st, hero_advice(st))
    heroes = ((89, "BG21_HERO_000", 10, 1), (95, "BG22_HERO_003", 12, 2))
    again = hero_select_state(heroes, seq=2)
    model.on_update(again, [offered_event(again, changed=1)])
    view = model.snapshot()
    assert view.rules == (Line("凯瑞尔·罗姆  护甲 10"), Line("范达尔·雷矛  护甲 12"))
    assert view.ai == ()  # 上一组的推荐不再显示
    model.show_hero(again, rules_pick(again))
    model.ai_pending("hero")
    view = model.snapshot()
    assert view.rules[0] == Line("★ 凯瑞尔·罗姆  护甲 10")
    assert view.ai == (Line("分析中…", "muted"),)


def test_after_the_choice_a_compact_title_until_recruit():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, rules_pick(st))
    model.show_hero(st, hero_advice(st))
    picked = hero_select_state(seq=3, chosen="BG21_HERO_000")
    model.on_update(picked, [Event(type=EventType.HERO_CHOSEN, turn=0)])
    view = model.snapshot()
    assert (view.title, view.compact) == ("已选 凯瑞尔·罗姆 · 等待招募阶段", True)
    model.on_update(T3.model_copy(update={"turn": 1}), started(1))
    view = model.snapshot()
    assert view.title.startswith("第 1 回合") and (view.rules, view.ai) == ((), ())


def test_new_game_drops_the_hero_advice():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, rules_pick(st))
    model.show_hero(st, hero_advice(st))
    choosing(model, st.model_copy(update={"seq": 9}))  # 重连：同一组英雄，重新开始
    assert (model.snapshot().rules, model.snapshot().ai) == (HERO_ROWS, ())


def test_hero_view_shows_the_uncertain_warning():
    model = OverlayModel(name)
    choosing(model, hero_select_state(uncertain=True))
    assert model.snapshot().warning == "⚠ 状态可能不准"


def test_concurrent_hero_calls_are_safe():
    model = OverlayModel(name)
    errors = []

    def writer(seq):
        try:
            for i in range(200):
                st = hero_select_state(seq=seq * 1000 + i)
                model.on_update(st, [offered_event(st)])
                model.show_hero(st, rules_pick(st))
                model.ai_pending("hero")
                model.show_hero(st, hero_advice(st, comments=COMMENTS), agrees=i % 2 == 0)
                model.ai_text("AI 超时", "hero")
                model.snapshot()
        except Exception as exc:  # pragma: no cover - 失败时才走到
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in (1, 2, 3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def test_new_offer_drops_the_old_hero_ai_lines():
    # 上一组的错误、提示不能留到新的一组上（新的请求会再给"分析中…"）
    model = OverlayModel(name)
    choosing(model)
    model.ai_text("AI 超时", "hero")
    heroes = ((89, "BG21_HERO_000", 10, 1), (95, "BG22_HERO_003", 12, 2))
    again = hero_select_state(heroes, seq=2)
    model.on_update(again, [offered_event(again, changed=1)])
    assert model.snapshot().ai == ()
    model.ai_text("生成 AI 局面时出错，本次跳过")
    model.on_update(again.model_copy(update={"seq": 3}), [])  # 别的更新：提示留着
    assert model.snapshot().ai == (Line("生成 AI 局面时出错，本次跳过", "muted"),)


def test_title_before_the_recruit_starts_leaves_out_the_upgrade():
    # TURN 变成 1 之后约 0.5 秒才有升本按钮：这时不显示"已满本"
    model = OverlayModel(name)
    early = make_state(turn=1, gold=0, upgrade_cost=None, main_action=False)
    model.on_update(early, [])
    assert model.snapshot().title == "第 1 回合 · 铸币 0 · 1 本"
    model.on_update(make_state(turn=1, gold=3, seq=2), started(1))
    assert model.snapshot().title == "第 1 回合 · 铸币 3 · 1 本 · 升本 5"


def test_hero_rules_error_keeps_the_hero_rows():
    model = OverlayModel(name)
    choosing(model)
    model.rules_error(hero=True)
    assert model.snapshot().rules == HERO_ROWS  # 英雄和护甲照样列出，只是没有 ★


# ---- 下一步 ----


def step(model):
    return model.snapshot().next_step


def test_next_step_follows_the_rules_plan_and_moves_on():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    assert step(model) == NextStep("▶ 等建议…", tone="muted")  # 还没有任何方案
    model.show_rules(T3, rules_advice())
    assert step(model) == NextStep(
        "▶ 买 左数第 1 个 海盗",
        Target(row="shop", index=0, count=2),
        slots="商店 ● ○",
        badge="1",
    )
    model.on_update(BOUGHT, [])  # 买了：换成第二步，编号是它在完整方案里的序号
    assert step(model) == NextStep("▶ 刷新商店", Target(button="refresh"), badge="2")
    model.on_update(REFRESHED, [])
    assert step(model) == NextStep("✓ 建议的步骤都做完了", tone="muted")


def test_next_step_switches_to_the_ai_plan_and_then_the_deep_one():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.ai_pending()
    assert step(model).text == "▶ 买 左数第 1 个 海盗"  # AI 分析中：先看规则
    model.show_ai(T3, ai_advice(), False)
    assert step(model) == NextStep(
        "▶ 买 左数第 2 个 蚀脑怪",
        Target(row="shop", index=1, count=2),
        slots="商店 ○ ●",
        badge="1",
    )
    model.show_ai(T3, llm_advice((Action(type="upgrade"),), lane="deep"), False)
    assert step(model) == NextStep("▶ 升本 → 3 本（6 金）", Target(button="upgrade"), badge="1")


def test_next_step_waits_for_new_ai_advice_after_a_refresh():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_ai(T3, ai_advice(), False)
    model.on_update(REFRESHED, [])
    assert step(model) == NextStep("▶ 等新的建议…", tone="muted")
    model.ai_text(BUDGET_EXHAUSTED)  # AI 不会再来：退回规则的方案
    assert step(model).text == "✓ 建议的步骤都做完了"


def test_next_step_for_an_expired_deep_plan_uses_the_fast_one():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", expired="invalid", **DEEP), False)
    model.show_ai(T3, ai_advice(), False)
    assert step(model).text == "▶ 买 左数第 2 个 蚀脑怪"


def test_next_step_for_each_kind_of_action():
    from factory import shop_spell

    hp = power(targets=(10,))
    state = T3.model_copy(
        update={
            "shop": (*SHOP, shop_spell(22, "S", 1)),
            "me": T3.me.model_copy(update={"hero_power": hp}),
        }
    )
    model = OverlayModel(lambda c: {"S": "香蕉"}.get(c, name(c)))
    model.on_update(state, started(3))

    def first_step(*actions):
        plan = Plan(source="rules", actions=tuple(actions))
        model.show_rules(state, Advice(state_seq=1, turn=3, plan=plan))
        return step(model)

    assert first_step(Action(type="buy", entity_id=22)) == NextStep(
        "▶ 买 左数第 3 个 香蕉（法术）",
        Target(row="shop", index=2, count=3),
        slots="商店 ○ ○ ●",
        badge="1",
    )
    assert first_step(Action(type="sell", entity_id=10)) == NextStep(
        "▶ 卖 左数第 1 个 小鬼", Target(row="board", index=0, count=1), slots="场面 ●", badge="1"
    )
    assert first_step(Action(type="freeze")) == NextStep(
        "▶ 冻结商店", Target(button="freeze"), badge="1"
    )
    assert first_step(Action(type="hero_power", entity_id=10)) == NextStep(
        "▶ 用英雄技能 定罪 → 小鬼", Target(button="hero_power"), badge="1"
    )
    assert first_step(Action(type="buy", entity_id=99)).target is None  # 不在商店里：只有文字
    assert first_step() == NextStep("无需操作", tone="muted")


def test_next_step_is_none_in_combat_and_without_a_game():
    model = OverlayModel(name)
    assert step(model) is None
    model.on_update(make_state(turn=3, phase=Phase.COMBAT), [])
    assert step(model) is None


def test_next_step_at_hero_select_prefers_the_ai_pick():
    model = OverlayModel(name)
    state = hero_select_state()
    model.on_update(state, [offered_event(state)])
    assert step(model) == NextStep("▶ 等建议…", tone="muted")
    signature = state.hero_select.signature()
    model.show_hero(state, HeroAdvice(source="rules", signature=signature, pick=89, reason="护甲"))
    assert step(model) == NextStep(
        "▶ 选 左数第 1 个 凯瑞尔·罗姆",
        Target(row="hero", index=0, count=4),
        slots="英雄 ● ○ ○ ○",
        badge="选",
    )
    ai = HeroAdvice(source="llm", signature=signature, pick=92, reason="稳")
    model.show_hero(state, ai)
    assert step(model).text == "▶ 选 左数第 4 个 了不起的杰弗里斯"
    assert step(model).target == Target(row="hero", index=3, count=4)


# ---- 选项 ----

CHOICE_NAMES = {"BG36_312": "摧心招募者", "BG33_318": "怒胆喷毒鱼人", "BG36_356": "泰瑞尔"}


def choice_name(card_id):
    return CHOICE_NAMES.get(card_id, name(card_id))


def offered_choice(model, **kwargs):
    from factory import choice_event, choice_state

    state = choice_state(**kwargs)
    model.on_update(state, [choice_event(state)])
    model.show_choice(state, None)
    return state


def choice_advice(pick=6933, choice_id=12, reason="32/32 直接成主力"):
    from lscs.advisor.models import ChoiceAdvice

    return ChoiceAdvice(choice_id=choice_id, pick=pick, reason=reason, elapsed=1.2)


def test_choice_section_and_next_step_with_ai():
    model = OverlayModel(choice_name)
    state = offered_choice(model)
    view = model.snapshot()
    assert view.choice == (
        Line("黑暗之赐 · 3 选 1"),
        Line("摧心招募者 · 怒胆喷毒鱼人 · 泰瑞尔", role="comment"),
    )
    assert view.next_step == NextStep("▶ 等 AI 选…", tone="muted")
    model.ai_pending("choice")
    assert model.snapshot().ai == (Line("分析中…", "muted"),)
    model.show_choice(state, choice_advice())
    view = model.snapshot()
    assert view.choice[1] == Line("摧心招募者 · 怒胆喷毒鱼人 · ★泰瑞尔", role="comment")
    assert view.next_step == NextStep(
        "▶ 选 左数第 3 个 泰瑞尔",
        Target(row="choice", index=2, count=3),
        slots="选项 ○ ○ ●",
        reason="32/32 直接成主力",
        badge="选",
    )
    assert view.ai == ()  # 结果到了：AI 区回到招募的内容（这里还没有）


def test_choice_without_ai_asks_the_player_to_choose():
    model = OverlayModel(choice_name, ai_enabled=False)
    offered_choice(model)
    assert step(model) == NextStep("▶ 自己选一个（AI 没有建议）", tone="muted")
    model = OverlayModel(choice_name, choice_ai=False)
    offered_choice(model)
    assert step(model).text == "▶ 自己选一个（AI 没有建议）"
    model = OverlayModel(choice_name)
    offered_choice(model)
    model.ai_pending("choice")
    model.ai_text("AI 超时", "choice")
    view = model.snapshot()
    assert view.ai == (Line("AI 超时", "muted"),)
    assert view.next_step.text == "▶ 自己选一个（AI 没有建议）"


def test_choice_section_disappears_once_chosen_and_ignores_stale_advice():
    model = OverlayModel(choice_name)
    state = offered_choice(model)
    model.show_choice(state, choice_advice(choice_id=11))  # 上一组的结果
    assert step(model).text == "▶ 等 AI 选…"
    model.show_choice(state, choice_advice())
    from factory import resolved_event

    done = make_state(turn=9, seq=2, shop=SHOP)
    model.on_update(done, [resolved_event(12, picked=6933)])
    view = model.snapshot()
    assert view.choice == ()
    assert view.next_step == NextStep("▶ 等建议…", tone="muted")
    offered_choice(model, seq=3)  # 同一组编号再出现也不会沿用旧的推荐（已经清空）
    assert step(model).text == "▶ 等 AI 选…"


def test_choice_before_main_action_starts_the_turn():
    model = OverlayModel(choice_name)
    model.on_update(make_state(turn=5, seq=1), started(5))
    model.show_rules(make_state(turn=5, seq=1), rules_advice(turn=5))
    offered_choice(model, turn=6, main_action=False, seq=2)
    view = model.snapshot()
    assert view.title == "第 6 回合 · 铸币 3 · 1 本"
    assert view.rules == ()  # 新回合：上一回合的建议清空
    assert view.next_step.text == "▶ 等 AI 选…"


def test_condense_keeps_the_next_step_and_drops_the_option_names():
    view = OverlayView(
        title="第 9 回合",
        compact=False,
        next_step=NextStep("▶ 选 左数第 3 个 泰瑞尔", slots="选项 ○ ○ ●", reason="理由"),
        choice=(Line("黑暗之赐 · 3 选 1"), Line("a · ★b · c", role="comment")),
    )
    assert condense(view, 1) == view
    two = condense(view, 2)
    assert two.next_step == view.next_step
    assert two.choice == (Line("黑暗之赐 · 3 选 1"),)


def test_concurrent_choice_updates_are_safe():
    from factory import choice_event, choice_state, resolved_event

    model = OverlayModel(choice_name)
    errors = []

    def writer(seq0):
        try:
            for i in range(200):
                state = choice_state(seq=seq0 + i)
                model.on_update(state, [choice_event(state)])
                model.ai_pending("choice")
                model.show_choice(state, choice_advice(pick=6931 + i % 3))
                model.on_update(make_state(turn=9, seq=seq0 + i), [resolved_event(12)])
                model.snapshot()
        except Exception as exc:  # pragma: no cover - 失败时才走到
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n * 1000,)) for n in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# ---- AI 自我改正（AI 自我改正设计文档第 3.7 节） ----


def test_fast_repair_shows_repairing_and_the_rules_step_meanwhile():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.ai_pending()
    model.ai_repairing("fast", "第 1 步：铸币不够")
    view = model.snapshot()
    assert view.ai == (Line("改正中…", "muted"),)
    assert view.next_step.text == "▶ 买 左数第 1 个 海盗"  # 还是按规则方案
    model.show_ai(T3, ai_advice(), False)
    assert model.snapshot().ai == (Line("① 买 蚀脑怪 3/4"),)


def test_deep_repair_keeps_the_deep_pending_line():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.ai_pending()
    model.ai_pending("deep")
    model.show_ai(T3, ai_advice(), False)
    before = model.snapshot()
    model.ai_repairing("deep", "第 2 步：刷新要放在最后，刷新之后不能再有动作")
    after = model.snapshot()
    assert after.ai == before.ai and after.ai[-1] == Line("深度分析中…", "muted")
    assert after.version == before.version  # 什么都没变
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    assert model.snapshot().ai_title == "AI（深度）"


def test_choice_repair_still_waits_for_the_ai():
    model = OverlayModel(choice_name)
    state = offered_choice(model)
    model.ai_pending("choice")
    model.ai_repairing("choice", "推荐的选项不在选项里")
    view = model.snapshot()
    assert view.ai == (Line("改正中…", "muted"),)
    assert view.next_step == NextStep("▶ 等 AI 选…", tone="muted")  # 不是"自己选一个"
    model.show_choice(state, choice_advice())
    assert step(model).text == "▶ 选 左数第 3 个 泰瑞尔"


def test_hero_repair_shows_repairing_until_the_pick_arrives():
    model = OverlayModel(name)
    st = choosing(model)
    model.ai_pending("hero")
    model.ai_repairing("hero", "推荐的英雄不在备选里")
    assert model.snapshot().ai == (Line("改正中…", "muted"),)
    model.show_hero(st, hero_advice(st))
    assert model.snapshot().ai[0] == Line("选 凯瑞尔·罗姆：0 费技能每回合白送属性")


def test_choice_lane_notice_without_budget_lets_the_player_choose():
    from lscs.llm.notices import NO_BUDGET_THIS_TIME

    model = OverlayModel(choice_name)
    offered_choice(model)
    model.ai_pending("choice")
    model.ai_text(NO_BUDGET_THIS_TIME, "choice")  # 调度器的分路提示（设计文档第 5.1 节）
    view = model.snapshot()
    assert view.ai == (Line("这次不再分析：本局 AI 预算已用完", "muted"),)
    assert view.next_step == NextStep("▶ 自己选一个（AI 没有建议）", tone="muted")


# ---- 对手一览、对子和要找的张数 ----

OPP_NAMES = {
    "H2": "洛卡拉",
    "H3": "厄祖玛特",
    "H4": "布莱恩",
    "H5": "詹迪斯",
    "H6": "范克里夫",
    "W": "飘荡祭品",
    "P": "夺金健将",
    "E": "电音合成师",
}
NAMES.update(OPP_NAMES)


def rival(player_id, health, armor, place, tier, board=(), seen=None):
    from lscs.state.models import OpponentView

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


GHOST_BOARD = (
    minion(50, "P", 7, 7, races=["PIRATE"], golden=True),
    minion(51, "E", 3, 5, tier=2, races=["DRAGON"]),
)
RIVALS = {
    2: rival(2, 30, 9, 1, 5, [minion(40 + i, "W", 10, 9, 3, ["ABERRATION"]) for i in (1, 2)], 9),
    3: rival(3, 30, 3, 3, 5, [minion(43, "C", 90, 90, 2, ["ABERRATION"])], 8),
    4: rival(4, 28, 0, 2, 4),
    5: rival(5, 0, 0, 8, 2, GHOST_BOARD, 4),  # 已淘汰，下一个对手是它的残影
    6: rival(6, -9, 0, 7, 5, [minion(44, "E", 3, 4)], 5),
}


def with_rivals(state, next_id=5, opponents=None):
    update = {"opponents": opponents or RIVALS, "next_opponent_id": next_id}
    return state.model_copy(update=update)


T10 = with_rivals(make_state(turn=10, gold=10, tier=6, upgrade_cost=None, shop=SHOP))
GHOST = Line("▶ 詹迪斯 已淘汰（残影）· 第 4 回合见 2 个 · 身材 22", role="opponent")
STRONGEST = Line("★ 厄祖玛特 30+3 · 5本 · 畸变×1 身材 180 · 威胁 93", role="threat")


def test_recruit_shows_the_next_opponent_and_the_strongest_at_the_bottom():
    model = OverlayModel(name)
    model.on_update(T10, started(10))
    assert model.snapshot().opponents == (GHOST, STRONGEST)
    model.on_update(with_rivals(T10, next_id=4).model_copy(update={"seq": 2}), [])
    assert model.snapshot().opponents == (
        Line("▶ 布莱恩 28+0 · 4本 · 还没见过阵容", role="opponent"),
        STRONGEST,
    )
    model.on_update(with_rivals(T10, next_id=3).model_copy(update={"seq": 3}), [])
    assert model.snapshot().opponents == (
        Line("▶★ 厄祖玛特 30+3 · 5本 · 畸变×1 身材 180 · 第 8 回合见 · 威胁 93", role="opponent"),
    )


def test_opponent_lines_stay_while_a_choice_is_open():
    from factory import make_choice

    model = OverlayModel(name)
    model.on_update(T10.model_copy(update={"choice": make_choice()}), started(10))
    view = model.snapshot()
    assert view.choice and view.opponents == (GHOST, STRONGEST)


def test_no_opponent_lines_when_switched_off_or_unknown():
    model = OverlayModel(name, opponents=False)
    model.on_update(T10, started(10))
    assert model.snapshot().opponents == ()
    model = OverlayModel(name)
    model.on_update(T3, started(3))  # 没有对手的数据
    assert model.snapshot().opponents == ()


def test_condense_drops_the_strongest_first_then_the_next_opponent():
    model = OverlayModel(name)
    model.on_update(T10, started(10))
    model.show_ai(T10, llm_advice(BUY_21, turn=10, **DEEP), False)
    view = model.snapshot()
    one = condense(view, 1)
    assert one.opponents == (GHOST,)
    assert "direction" not in {line.role for line in one.ai}
    two = condense(view, 2)
    assert two.opponents == ()
    assert [line.text for line in two.ai] == ["① 买 蚀脑怪 3/4", "目标：提前升 5 本找畸变核心"]
    assert two.next_step == view.next_step


COMBAT_10 = with_rivals(make_state(turn=10, phase=Phase.COMBAT, seq=5))


def test_combat_lists_every_opponent():
    model = OverlayModel(name)
    model.on_update(COMBAT_10, [Event(type=EventType.COMBAT_STARTED, turn=10)])
    view = model.snapshot()
    assert (view.title, view.compact, view.next_step) == (
        "第 10 回合 · 战斗中 · 对手 詹迪斯",
        False,
        None,
    )
    assert (view.rules, view.ai) == ((), ())
    assert view.opponents == (
        Line("▶ 第8名 詹迪斯 已淘汰（残影）· 第 4 回合见 2 个 · 身材 22"),
        Line("第1名 洛卡拉 30+9 · 5本 · 畸变×2 身材 38 · 第 9 回合见", role="opponent"),
        Line("第2名 布莱恩 28+0 · 4本 · 还没见过阵容", role="opponent"),
        Line("★ 第3名 厄祖玛特 30+3 · 5本 · 畸变×1 身材 180 · 威胁 93", role="threat"),
        Line("第7名 范克里夫 已淘汰", "muted", role="comment"),
    )
    two = condense(view, 2)
    assert two.opponents == view.opponents[:1]  # 正在打的那一行总是保留


def test_combat_row_says_this_turn_once_the_board_is_captured():
    model = OverlayModel(name)
    model.on_update(COMBAT_10, [Event(type=EventType.COMBAT_STARTED, turn=10)])
    seen = dict(RIVALS)
    seen[5] = rival(5, 0, 0, 8, 2, GHOST_BOARD[:1], 10)  # 第一次攻击时截到本回合的阵容
    model.on_update(with_rivals(COMBAT_10, opponents=seen).model_copy(update={"seq": 6}), [])
    first = model.snapshot().opponents[0]
    assert first == Line("▶ 第8名 詹迪斯 已淘汰（残影）· 本回合见 1 个 · 身材 14")


def test_combat_strongest_being_fought_is_one_main_row():
    model = OverlayModel(name)
    model.on_update(with_rivals(COMBAT_10, next_id=3), [])
    rows = model.snapshot().opponents
    assert rows[0] == Line("▶★ 第3名 厄祖玛特 30+3 · 5本 · 畸变×1 身材 180 · 第 8 回合见 · 威胁 93")
    assert [line.role for line in rows] == ["main", "opponent", "opponent", "comment", "comment"]


def test_combat_stays_one_line_when_opponents_are_off_or_unknown():
    model = OverlayModel(name, opponents=False)
    model.on_update(COMBAT_10, [])
    view = model.snapshot()
    assert (view.title, view.compact, view.opponents) == ("第 10 回合 · 战斗中", True, ())
    model = OverlayModel(name)
    model.on_update(make_state(turn=3, phase=Phase.COMBAT), [])
    assert (model.snapshot().title, model.snapshot().compact) == ("第 3 回合 · 战斗中", True)


def pool_note(card_id, card_name, have, left, shop=0):
    from lscs.advisor.pool import PoolNote

    return PoolNote(card_id, card_name, have, left, shop)


def test_pairs_line_in_the_rules_and_pool_counts_after_look_for():
    notes = (
        pool_note("C", "蚀脑怪", 2, 11),
        pool_note("B", "海盗", 1, 14, shop=1),
        pool_note("W", "飘荡祭品", 0, 11),
    )
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice().model_copy(update={"pool_notes": notes}))
    assert model.snapshot().rules == (
        Line("① 买 海盗 3/2  ② 刷新"),
        Line("对子：蚀脑怪×2 · 还剩约 11 张；海盗×1 商店有 · 还剩约 14 张", role="look_for"),
        Line("方向：海盗", role="direction"),
    )
    advice = llm_advice(BUY_21, look_for=("飘荡祭品", "深渊使者"), summary="找祭品")
    model.show_ai(T3, advice.model_copy(update={"pool_notes": notes}), False)
    assert model.snapshot().ai == (
        Line("① 买 蚀脑怪 3/4"),
        Line("要找：飘荡祭品（约 11 张）、深渊使者", role="look_for"),
        Line("目标：找祭品", role="reason"),
    )
    view = model.snapshot()
    assert "对子" not in " ".join(line.text for line in condense(view, 2).rules)


def test_empty_captured_board_on_the_panel():
    seen_empty = {
        2: rival(2, 20, 0, 1, 3, (), 5),
        5: rival(5, 0, 0, 8, 2, (), 4),
    }
    model = OverlayModel(name)
    model.on_update(with_rivals(T10, next_id=5, opponents=seen_empty), started(10))
    assert model.snapshot().opponents == (
        Line("▶ 詹迪斯 已淘汰（残影）· 第 4 回合见到空场", role="opponent"),
        Line("★ 洛卡拉 20+0 · 3本 · 见到空场 · 威胁 35", role="threat"),
    )
    model.on_update(with_rivals(COMBAT_10, next_id=2, opponents=seen_empty), [])
    assert model.snapshot().opponents[0] == Line(
        "▶★ 第1名 洛卡拉 20+0 · 3本 · 第 5 回合见到空场 · 威胁 35"
    )


def test_look_for_with_pool_counts_is_cut_between_cards():
    notes = (
        pool_note("W", "飘荡祭品", 0, 11),
        pool_note("C", "蚀脑怪", 0, 11),
        pool_note("D", "深渊使者", 0, 12),
        pool_note("M", "残忍的精神鞭笞者", 0, 12),
    )
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    look_for = ("飘荡祭品", "蚀脑怪", "深渊使者", "残忍的精神鞭笞者")
    advice = llm_advice(BUY_21, look_for=look_for).model_copy(update={"pool_notes": notes})
    model.show_ai(T3, advice, False)
    assert model.snapshot().ai[1] == Line(
        "要找：飘荡祭品（约 11 张）、蚀脑怪（约 11 张）、深渊使者（约 12 张）…", role="look_for"
    )
    one = llm_advice(BUY_21, look_for=("长" * 50,))
    model.show_ai(T3, one, False)
    assert model.snapshot().ai[1].text == "要找：" + "长" * 37 + "…"  # 一张就放不下：按字截
