import threading

from factory import make_state, minion, shop_minion

from lscs.advisor.models import Action, Advice, Plan
from lscs.llm.notices import AUTH_DISABLED, BUDGET_EXHAUSTED
from lscs.overlay.model import IDLE, Line, OverlayModel, OverlayView, condense
from lscs.state.models import Event, EventType, Phase

NAMES = {"A": "小鬼", "B": "海盗", "C": "蚀脑怪"}


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
    assert model.snapshot().ai == (Line("① 买 蚀脑怪 3/4"), Line("思路：先保血", role="reason"))

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
        Line("① 刷新  ② 刷新  ③ 刷新  ④ 刷新  …"),
        Line("思路：" + "先" * 37 + "…", role="reason"),  # 整行最多 40 字，超出加"…"
        Line("基于 6 秒前的局面", "muted", role="note"),
    )
    assert len(ai[1].text) == 41


def test_summary_that_fits_is_not_cut():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(summary="先" * 37), False)
    assert model.snapshot().ai[-1] == Line("思路：" + "先" * 37, role="reason")


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
    assert model.snapshot().ai == (Line("① 买 蚀脑怪 3/4"), Line("思路：两张都要", role="reason"))
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
    hero = make_state(turn=0, phase=Phase.HERO_SELECT, seq=9)
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


def test_condense_drops_direction_first_then_notes():
    view = OverlayView(
        title="第 3 回合",
        compact=False,
        rules=(Line("① 刷新"), Line("方向：海盗", role="direction")),
        ai=(
            Line("① 买 海盗 3/2"),
            Line("方向：纳迦", role="direction"),
            Line("思路：先保血", role="reason"),
            Line("基于 6 秒前的局面", "muted", role="note"),
        ),
        warning="⚠ 状态可能不准",
    )
    assert condense(view, 0) == view
    one = condense(view, 1)
    assert one.rules == (Line("① 刷新"),)
    assert [line.text for line in one.ai] == ["① 买 海盗 3/2", "思路：先保血", "基于 6 秒前的局面"]
    two = condense(view, 2)
    assert [line.text for line in two.ai] == ["① 买 海盗 3/2", "思路：先保血"]
    assert (two.title, two.warning) == (view.title, view.warning)
