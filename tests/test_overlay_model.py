import threading

import pytest
from factory import hero_select_state, make_state, minion, offered_event, power, shop_minion
from helpers import NEEDS_STATS_FIXTURES

from lscs.advisor.models import Action, Advice, HeroAdvice, HeroComment, Plan
from lscs.llm.notices import AUTH_DISABLED, BUDGET_EXHAUSTED
from lscs.overlay.model import (
    IDLE,
    Block,
    Line,
    NextStep,
    OverlayModel,
    OverlayView,
    Target,
    condense,
    reason_lines,
)
from lscs.state.models import Event, EventType, Phase

# 和模式无关的用例两种模式各跑一遍：精简（默认）和详细（[overlay] detail = true）
MODES = pytest.mark.parametrize("detail", [False, True], ids=["compact", "detail"])

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


def rows(view):
    """精简模式的行：(标签, 正文)。"""
    return [(line.label, line.text) for block in view.blocks for line in block.lines]


def labeled(view, label):
    """标签是 label 的那一行的正文；没有时为 None。"""
    return next((text for tag, text in rows(view) if tag == label), None)


def texts(view):
    """所有块里的行的正文。"""
    return [line.text for block in view.blocks for line in block.lines]


def head(view):
    """页眉右侧：(来源, 后面的灰字)。"""
    return view.source, view.source_note


@MODES
def test_starts_idle(detail):
    view = OverlayModel(name, detail=detail).snapshot()
    assert view.title == IDLE == "lscs 已就绪，等待招募阶段"
    assert view.compact


def test_a_whole_turn():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    view = model.snapshot()
    assert view.title == "第 3 回合 · 铸币 5 · 2 本 · 升本 6"
    assert not view.compact
    assert (view.blocks, view.notice, view.warning, head(view)) == ((), None, None, ("", ""))
    assert view.next_step == NextStep("▶ 等建议…", tone="muted")

    model.show_rules(T3, rules_advice())
    view = model.snapshot()
    assert head(view) == ("规则", "")
    assert view.next_step.text == "▶ 买 左数第 1 个 海盗"
    assert view.blocks == (
        Block(
            "lines",
            (
                Line("② 刷新", "dim", "aside", label="然后", max_lines=2),
                Line("海盗", role="goal", label="方向", max_lines=1),
            ),
        ),
    )

    model.ai_pending()
    assert head(model.snapshot()) == ("规则", "· 分析中…")

    model.show_ai(T3, ai_advice(summary="先保血"), agrees=False)
    view = model.snapshot()
    assert head(view) == ("AI", "")
    assert view.next_step.text == "▶ 买 左数第 2 个 蚀脑怪"
    # 只显示 AI 这一套：规则的"然后"和"方向"都不在面板上（终端里有）
    assert view.blocks == (
        Block("lines", (Line("先保血", role="goal", label="目标", max_lines=2),)),
    )

    combat = make_state(turn=3, phase=Phase.COMBAT, seq=2)
    model.on_update(combat, [Event(type=EventType.COMBAT_STARTED, turn=3)])
    view = model.snapshot()
    assert (view.title, view.compact) == ("第 3 回合 · 战斗中", True)

    model.on_update(make_state(turn=4, gold=6, seq=3), started(4))
    view = model.snapshot()
    assert view.title.startswith("第 4 回合 · 铸币 6")
    assert (view.blocks, head(view)) == ((), ("", ""))  # 新回合清空上一回合的建议

    over = make_state(turn=4, phase=Phase.GAME_OVER, seq=4).model_copy(update={"final_place": 3})
    model.on_update(over, [Event(type=EventType.GAME_ENDED, turn=4, data={"place": 3})])
    view = model.snapshot()
    assert (view.title, view.compact) == ("对局结束 · 第 3 名", True)


@MODES
def test_advice_shown_before_the_state_update_of_the_same_turn_is_kept(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(make_state(turn=2, seq=1), started(2))
    model.show_rules(T3, rules_advice(turn=3))  # 建议先到，状态更新后到
    model.on_update(T3, started(3))
    assert model.snapshot().next_step.text == "▶ 买 左数第 1 个 海盗"


@MODES
def test_agreeing_ai_lists_its_own_steps(detail):
    # AI 同意规则时面板照样按它的步骤列，不另说"同意"（终端里说）
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(summary="稳"), agrees=True)
    view = model.snapshot()
    assert (view.next_step.text, head(view)) == ("▶ 买 左数第 2 个 蚀脑怪", ("AI", ""))
    assert "同意" not in str(view)


def test_long_summary_and_many_steps_and_no_stale_note():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    steps = tuple(Action(type="refresh") for _ in range(6))
    summary = "先" * 43
    model.show_ai(T3, ai_advice(summary=summary, stale="基于 6 秒前的局面", actions=steps), False)
    view = model.snapshot()
    # 剩下的五步都列在"然后"里，界面按像素折成最多两行
    assert labeled(view, "然后") == "② 刷新  ③ 刷新  ④ 刷新  ⑤ 刷新  ⑥ 刷新"
    assert labeled(view, "目标") == "先" * 40 + "…"  # 最多 40 字，超出加"…"
    assert "基于 6 秒前的局面" not in str(view)  # 附注只在终端里


def test_summary_that_fits_is_not_cut():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(summary="先" * 40), False)
    assert labeled(model.snapshot(), "目标") == "先" * 40


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
    view = model.snapshot()
    assert view.next_step == NextStep("▶ 刷新商店", Target(button="refresh"), badge="2")
    assert rows(view) == [("方向", "海盗")]  # 没有剩下的步：没有"然后"
    model.on_update(REFRESHED, [])
    view = model.snapshot()
    assert view.next_step == NextStep("✓ 建议的步骤都做完了", tone="muted")
    assert rows(view) == [("方向", "海盗")]


def test_rules_after_refresh_keep_steps_that_still_apply():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    plan = Plan(source="rules", actions=(Action(type="upgrade"), Action(type="buy", entity_id=20)))
    model.show_rules(T3, Advice(state_seq=1, turn=3, plan=plan))
    rich = T3.me.model_copy(update={"gold": 6})  # 升本要 6 金：做得起
    model.on_update(T3.model_copy(update={"shop": REFRESHED.shop, "seq": 2, "me": rich}), [])
    view = model.snapshot()  # 等规则的新建议
    assert view.next_step == NextStep("▶ 升本 → 3 本（6 金）", Target(button="upgrade"), badge="1")
    assert rows(view) == []


def test_ai_after_refresh_waits_for_new_advice_and_keeps_the_goal():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(summary="买蚀脑怪"), False)
    model.on_update(REFRESHED, [])
    view = model.snapshot()
    assert view.next_step == NextStep("▶ 等新的建议…", tone="muted")
    assert head(view) == ("", "分析中…")
    assert rows(view) == [("目标", "买蚀脑怪")]


@MODES
def test_ai_after_refresh_when_ai_is_off_says_so_at_the_bottom(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(), False)
    model.ai_text(BUDGET_EXHAUSTED)  # 之后不会再有 AI 建议
    model.show_ai(T3, ai_advice(), False)
    model.on_update(REFRESHED, [])
    view = model.snapshot()
    assert view.next_step == NextStep("▶ 等建议…", tone="muted")  # 等规则的新建议
    assert view.notice == "本局 AI 次数已用完"


def test_plan_without_steps_shows_no_action_needed():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    empty = Plan(source="rules", summary="本回合不需要操作", direction="海盗")
    model.show_rules(T3, Advice(state_seq=1, turn=3, plan=empty))
    view = model.snapshot()
    assert view.next_step == NextStep("无需操作", tone="muted")
    assert rows(view) == [("方向", "海盗")]


def test_ai_steps_already_done_are_dropped():
    rich = BOUGHT.model_copy(update={"me": BOUGHT.me.model_copy(update={"gold": 5})})
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    actions = (Action(type="buy", entity_id=20), Action(type="buy", entity_id=21))
    model.show_ai(T3, ai_advice(summary="两张都要", actions=actions), False)
    assert labeled(model.snapshot(), "然后") == "② 买 蚀脑怪"
    model.on_update(rich, [])
    view = model.snapshot()
    assert view.next_step.text == "▶ 买 左数第 1 个 蚀脑怪"
    assert view.next_step.badge == "2"
    assert rows(view) == [("目标", "两张都要")]
    model.on_update(rich.model_copy(update={"shop": (), "seq": 4}), [])
    view = model.snapshot()
    assert view.next_step.text == "✓ 建议的步骤都做完了"
    assert rows(view) == [("目标", "两张都要")]


@MODES
def test_ai_switched_off_mid_game_stays_at_the_bottom(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.ai_text(BUDGET_EXHAUSTED)
    assert model.snapshot().notice == "本局 AI 次数已用完"
    model.on_update(make_state(turn=4, seq=5), started(4))
    assert model.snapshot().notice == "本局 AI 次数已用完"
    new_game = [Event(type=EventType.GAME_STARTED, turn=0)]
    model.on_update(make_state(turn=0, phase=Phase.HERO_SELECT, seq=6), new_game)
    model.on_update(make_state(turn=1, seq=7), started(1))
    assert model.snapshot().notice is None  # 预算按局计算，新的一局恢复

    model.ai_text(AUTH_DISABLED)
    model.on_update(make_state(turn=2, seq=8), started(2))
    assert model.snapshot().notice == "AI 已关闭（Key 无效）"
    model.on_update(make_state(turn=0, phase=Phase.HERO_SELECT, seq=9), new_game)
    model.on_update(make_state(turn=1, seq=10), started(1))
    assert model.snapshot().notice == "AI 已关闭（Key 无效）"  # Key 无效一直有效


@MODES
def test_notice_priority_ai_off_then_ai_error_then_rules_error(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.rules_error()
    assert model.snapshot().notice == "本回合规则建议出错"
    model.ai_text("AI 超时")
    assert model.snapshot().notice == "AI 超时"
    model.ai_text(BUDGET_EXHAUSTED)
    assert model.snapshot().notice == "本局 AI 次数已用完"


@MODES
def test_notice_hides_ai_trouble_while_an_ai_plan_is_shown(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    model.ai_text("AI 超时")  # 快速建议这一路出错
    model.ai_text(BUDGET_EXHAUSTED)
    assert model.snapshot().source == "AI 深度"
    assert model.snapshot().notice is None
    model.rules_error()
    assert model.snapshot().notice == "本回合规则建议出错"


@MODES
def test_ai_disabled_and_uncertain_and_full_tier(detail):
    model = OverlayModel(name, ai_enabled=False, detail=detail)
    state = make_state(turn=12, gold=10, tier=6, upgrade_cost=None)  # 满本时没有升本按钮
    model.on_update(state.model_copy(update={"uncertain": True}), started(12))
    view = model.snapshot()
    assert view.title == "第 12 回合 · 铸币 10 · 6 本 · 已满本"
    assert view.notice is None  # 不写"AI 未开启"（启动时终端说过）
    assert view.warning == "⚠ 状态可能不准"
    model.show_rules(state, rules_advice(turn=12))
    assert model.snapshot().source == "规则"


@MODES
def test_waiting_hero_select_and_new_game(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    # 还没发备选英雄：英雄还是占位的
    hero = make_state(turn=0, phase=Phase.HERO_SELECT, seq=9, hero="TB_BaconShop_HERO_PH")
    model.on_update(hero, [Event(type=EventType.GAME_STARTED, turn=0)])
    assert model.snapshot().title == IDLE
    model.on_update(T3, started(3))  # 新对局的同一个回合数，不能留着上一局的建议
    view = model.snapshot()
    assert (view.blocks, view.next_step.text) == ((), "▶ 等建议…")
    over = make_state(phase=Phase.GAME_OVER)
    model.on_update(over, [])
    assert model.snapshot().title == "对局结束"


@MODES
def test_version_changes_only_when_content_changes(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    first = model.snapshot().version
    model.on_update(T3.model_copy(update={"seq": 5}), [])  # 内容没变
    assert model.snapshot().version == first
    model.show_rules(T3, rules_advice())
    assert model.snapshot().version == first + 1


@MODES
def test_message_overrides_everything(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_message("监听已停止，请查看终端")
    view = model.snapshot()
    assert (view.title, view.compact) == ("监听已停止，请查看终端", True)


def test_ai_summary_is_accepted():
    OverlayModel(name).ai_summary(3, 1000, 200)  # 面板不显示，但必须有这个方法


@MODES
def test_concurrent_calls_are_safe(detail):
    model = OverlayModel(name, detail=detail)
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


def test_condense_levels_in_compact_mode():
    step = NextStep("▶ 选 左数第 3 个 泰瑞尔", slots="选项 ○ ○ ●", reason="理由")
    then = Line("② 买 海盗  ③ 刷新", "dim", "aside", label="然后", max_lines=2)
    goal = Line("先保血再找畸变", role="goal", label="目标", max_lines=2)
    view = OverlayView(
        title="第 3 回合",
        compact=False,
        next_step=step,
        source="AI",
        blocks=(Block("lines", (then, goal)),),
        notice="本回合规则建议出错",
        warning="⚠ 状态可能不准",
    )
    assert condense(view, 0) == view
    one = condense(view, 1)
    assert one.blocks == (Block("lines", (goal,)),)  # 次要的"然后"先去掉
    two = condense(view, 2)
    assert two.blocks == (
        Block("lines", (Line(goal.text, role="goal", label="目标", max_lines=1),)),
    )
    assert condense(view, 3) == two  # 精简模式的第 3 级什么都不做
    for level in (1, 2, 3):
        kept = condense(view, level)
        assert (kept.title, kept.next_step, kept.notice, kept.warning, kept.source) == (
            view.title,
            step,
            view.notice,
            view.warning,
            "AI",
        )
    # "下一步"的理由：第 2 级起只留一行
    assert [reason_lines(level) for level in range(4)] == [2, 2, 1, 1]
    # 块里的行都去掉了，块也不留
    only_then = OverlayView(title="t", compact=False, blocks=(Block("lines", (then,)),))
    assert condense(only_then, 1).blocks == ()


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
    assert labeled(model.snapshot(), "然后") == "② 用技能 定罪  ③ 刷新"  # 短写
    used = BOUGHT.model_copy(
        update={"me": BOUGHT.me.model_copy(update={"hero_power": power(used=True)}), "seq": 5}
    )
    model.on_update(used, [])
    view = model.snapshot()
    assert view.next_step == NextStep("▶ 刷新商店", Target(button="refresh"), badge="3")
    assert labeled(view, "然后") is None


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


def test_goal_is_the_summary_not_the_long_analysis_and_no_look_for():
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
    assert rows(view) == [("目标", "补一张战力，留钱刷新")]  # 要找、方向只在终端里


def test_analysis_is_shown_only_when_there_is_no_summary():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_21, analysis="分" * 50), False)
    assert labeled(model.snapshot(), "目标") == "分" * 40 + "…"


def test_goal_falls_back_to_the_rules_direction_only_without_any_ai_plan():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice(direction=None))
    assert rows(model.snapshot()) == [("然后", "② 刷新")]  # 没有 AI、也没有方向：没有这一行
    model.show_rules(T3, rules_advice())
    assert labeled(model.snapshot(), "方向") == "海盗"
    model.show_ai(T3, llm_advice(BUY_21), False)  # AI 的方案没有总结和分析
    view = model.snapshot()
    assert labeled(view, "方向") is None and labeled(view, "目标") is None


def test_deep_analysis_pending_and_deep_result_first():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.ai_pending()
    model.ai_pending("deep")
    assert head(model.snapshot()) == ("", "分析中…")
    model.show_rules(T3, rules_advice())
    assert head(model.snapshot()) == ("规则", "· 分析中…")
    model.show_ai(T3, llm_advice(BUY_21, analysis="快速分析"), False)
    assert head(model.snapshot()) == ("AI", "· 分析中…")  # 深度分析还在等
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    view = model.snapshot()
    assert head(view) == ("AI 深度", "")
    assert view.next_step.text == "▶ 买 左数第 1 个 海盗"
    assert rows(view) == [("目标", "提前升 5 本找畸变核心")]  # 规则的步骤不在面板上
    model.on_update(make_state(turn=4, seq=9), started(4))
    view = model.snapshot()
    assert (view.blocks, head(view)) == ((), ("", ""))  # 新回合清空深度分析


def test_deep_steps_done_keep_the_thoughts():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    model.on_update(BOUGHT, [])
    view = model.snapshot()
    assert view.next_step.text == "✓ 建议的步骤都做完了"
    assert rows(view) == [("目标", "提前升 5 本找畸变核心")]


def test_expired_deep_result_gives_the_goal_to_the_fast_plan():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    plan = Plan(source="llm", actions=BUY_21, analysis="快速分析", direction="海盗")
    fast = Advice(state_seq=1, turn=3, plan=plan, stale_note="基于 6 秒前的局面", elapsed=2.0)
    model.show_ai(T3, fast, False)
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", expired="invalid", **DEEP), False)
    view = model.snapshot()
    assert head(view) == ("AI", "")  # 快速建议的操作
    assert view.next_step.text == "▶ 买 左数第 2 个 蚀脑怪"
    assert rows(view) == [("目标", "提前升 5 本找畸变核心")]  # 深度分析的思路


RICH_REFRESHED = REFRESHED.model_copy(update={"me": REFRESHED.me.model_copy(update={"gold": 5})})


def test_refresh_after_deep_result_keeps_its_thoughts_with_the_new_fast_advice():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_21, analysis="快速分析"), False)
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    model.on_update(RICH_REFRESHED, [])  # 深度分析的操作过期
    view = model.snapshot()
    assert (view.next_step.text, head(view)) == ("▶ 等新的建议…", ("", "分析中…"))
    assert rows(view) == [("目标", "提前升 5 本找畸变核心")]
    buy_30 = (Action(type="buy", entity_id=30),)
    advice = llm_advice(buy_30, state_seq=3, summary="刷新后的快速建议")
    model.show_ai(RICH_REFRESHED, advice, False)
    view = model.snapshot()
    assert (view.next_step.text, head(view)) == ("▶ 买 左数第 1 个 小鬼", ("AI", ""))
    assert rows(view) == [("目标", "提前升 5 本找畸变核心")]  # 深度优先


def test_expired_deep_thoughts_while_the_new_fast_advice_is_pending():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_21, analysis="快速分析"), False)
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    model.on_update(RICH_REFRESHED, [])
    model.ai_pending()  # 刷新后的快速建议已经发出
    view = model.snapshot()
    assert (view.next_step.text, head(view)) == ("▶ 等建议…", ("", "分析中…"))
    assert rows(view) == [("目标", "提前升 5 本找畸变核心")]


@MODES
def test_deep_error_only_clears_the_pending_note(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.ai_pending("deep")
    model.show_ai(T3, llm_advice(BUY_21), False)
    assert head(model.snapshot()) == ("AI", "· 分析中…")
    model.ai_text("深度分析超时", "deep")
    view = model.snapshot()
    assert (head(view), view.notice) == (("AI", ""), None)  # 深度分析的错误只在终端里


def test_deep_agreeing_with_rules_is_shown_as_the_deep_plan():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), True)
    view = model.snapshot()
    assert (view.next_step.text, head(view)) == ("▶ 买 左数第 1 个 海盗", ("AI 深度", ""))
    assert rows(view) == [("目标", "提前升 5 本找畸变核心")]


@MODES
def test_budget_notice_with_deep_pending(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.ai_text(BUDGET_EXHAUSTED)
    model.ai_pending("deep")
    view = model.snapshot()
    assert (view.notice, head(view)) == ("本局 AI 次数已用完", ("", "分析中…"))


def test_only_one_plan_is_shown_and_falls_back_in_order():
    # 规则 + 快速 + 深度都在：只显示深度；深度过期 → 快速；快速的商店刷新了 → 等新的建议
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_ai(T3, llm_advice((*BUY_21, Action(type="refresh")), summary="快"), False)
    deep = llm_advice((*BUY_20, Action(type="upgrade")), lane="deep", **DEEP)
    model.show_ai(T3, deep, False)
    view = model.snapshot()
    assert head(view) == ("AI 深度", "")
    assert rows(view) == [("然后", "② 升本 → 3 本"), ("目标", "提前升 5 本找畸变核心")]
    model.show_ai(T3, deep.model_copy(update={"expired": "refreshed"}), False)
    view = model.snapshot()
    assert head(view) == ("AI", "")
    assert rows(view) == [("然后", "② 刷新"), ("目标", "提前升 5 本找畸变核心")]
    model.on_update(RICH_REFRESHED, [])
    view = model.snapshot()
    assert (view.next_step.text, head(view)) == ("▶ 等新的建议…", ("", "分析中…"))


# ---- 选英雄 ----

HERO_TITLE = "选英雄 · 护甲越少越强"
HERO_ARMOR = "凯瑞尔·罗姆 10 · 泰瑟兰·血望者 18 · 洛，在世传奇 19 · 了不起的杰弗里斯 17"
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
    assert (view.title, view.compact, view.warning, head(view)) == (
        HERO_TITLE,
        False,
        None,
        ("", ""),
    )
    assert view.next_step == NextStep("▶ 等建议…", tone="muted")
    assert rows(view) == [("护甲", HERO_ARMOR)]  # 规则建议还没到：先列出英雄，不标 ★
    model.show_hero(st, rules_pick(st))
    view = model.snapshot()
    assert rows(view) == [("护甲", "★" + HERO_ARMOR)]
    assert head(view) == ("规则", "")
    model.ai_pending("hero")
    assert head(model.snapshot()) == ("规则", "· 分析中…")
    model.show_hero(st, hero_advice(st, pick=90, comments=COMMENTS))
    view = model.snapshot()
    assert head(view) == ("AI", "")
    assert view.next_step == NextStep(
        "▶ 选 左数第 2 个 泰瑟兰·血望者",
        Target(row="hero", index=1, count=4),
        slots="英雄 ○ ● ○ ○",
        reason="0 费技能每回合白送属性",
        badge="选",
    )
    # ★ 跟着"下一步"（AI 的推荐）；点评只在终端和详细模式里
    assert rows(view) == [
        ("护甲", "凯瑞尔·罗姆 10 · ★泰瑟兰·血望者 18 · 洛，在世传奇 19 · 了不起的杰弗里斯 17")
    ]
    assert "要靠对子才强" not in str(view)


def test_hero_rules_without_a_pick_and_ai_agreeing():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, rules_pick(st, pick=None))
    view = model.snapshot()
    assert (view.title, rows(view), view.next_step.text) == (
        HERO_TITLE,
        [("护甲", HERO_ARMOR)],
        "▶ 等建议…",
    )
    model.show_hero(st, hero_advice(st, pick=91, comments=((91, "稳"),)), agrees=True)
    view = model.snapshot()
    assert view.next_step.text == "▶ 选 左数第 3 个 洛，在世传奇"
    assert view.next_step.reason == "0 费技能每回合白送属性"  # 不再写"✓ 也选 ××："
    assert "★洛，在世传奇 19" in labeled(view, "护甲")


def test_hero_reason_is_the_whole_ai_reason():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, hero_advice(st, reason="理" * 60))
    assert model.snapshot().next_step.reason == "理" * 60  # 界面按像素截成最多两行


def test_hero_unknown_armor():
    heroes = ((89, "BG21_HERO_000", 0, 1), (95, "BG22_HERO_003", 12, 2))
    model = OverlayModel(name)
    choosing(model, hero_select_state(heroes))
    assert labeled(model.snapshot(), "护甲") == "凯瑞尔·罗姆 未知 · 范达尔·雷矛 12"


@MODES
def test_hero_errors_and_ai_off_go_to_the_bottom(detail):
    model = OverlayModel(name, detail=detail)
    choosing(model)
    model.ai_text("AI 超时", "hero")
    assert model.snapshot().notice == "AI 超时"
    model.ai_text(BUDGET_EXHAUSTED)  # 调度器的提示不分路：选英雄时算选英雄这一路的
    assert model.snapshot().notice == "本局 AI 次数已用完"
    model.on_update(make_state(turn=1, seq=5), started(1))
    assert model.snapshot().notice == "本局 AI 次数已用完"
    off = OverlayModel(name, ai_enabled=False, detail=detail)
    choosing(off)
    assert off.snapshot().notice is None  # 不写"AI 未开启"
    disabled = OverlayModel(name, detail=detail)
    disabled.ai_text(AUTH_DISABLED)
    choosing(disabled)  # 新的一局也一直有效
    assert disabled.snapshot().notice == "AI 已关闭（Key 无效）"


def test_condense_drops_the_hero_row_first():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, hero_advice(st, comments=COMMENTS))
    view = model.snapshot()
    one = condense(view, 1)
    assert one.blocks == () and one.next_step == view.next_step


def test_reroll_shows_the_new_heroes_and_waits_for_the_ai_again():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, rules_pick(st))
    model.show_hero(st, hero_advice(st))
    heroes = ((89, "BG21_HERO_000", 10, 1), (95, "BG22_HERO_003", 12, 2))
    again = hero_select_state(heroes, seq=2)
    model.on_update(again, [offered_event(again, changed=1)])
    view = model.snapshot()
    assert rows(view) == [("护甲", "凯瑞尔·罗姆 10 · 范达尔·雷矛 12")]
    assert head(view) == ("", "")  # 上一组的推荐不再显示
    model.show_hero(again, rules_pick(again))
    model.ai_pending("hero")
    view = model.snapshot()
    assert labeled(view, "护甲").startswith("★凯瑞尔·罗姆 10")
    assert head(view) == ("规则", "· 分析中…")


@MODES
def test_after_the_choice_a_compact_title_until_recruit(detail):
    model = OverlayModel(name, detail=detail)
    st = choosing(model)
    model.show_hero(st, rules_pick(st))
    model.show_hero(st, hero_advice(st))
    picked = hero_select_state(seq=3, chosen="BG21_HERO_000")
    model.on_update(picked, [Event(type=EventType.HERO_CHOSEN, turn=0)])
    view = model.snapshot()
    assert (view.title, view.compact) == ("已选 凯瑞尔·罗姆 · 等待招募阶段", True)
    model.on_update(T3.model_copy(update={"turn": 1}), started(1))
    view = model.snapshot()
    assert view.title.startswith("第 1 回合") and view.blocks == ()


def test_new_game_drops_the_hero_advice():
    model = OverlayModel(name)
    st = choosing(model)
    model.show_hero(st, rules_pick(st))
    model.show_hero(st, hero_advice(st))
    choosing(model, st.model_copy(update={"seq": 9}))  # 重连：同一组英雄，重新开始
    view = model.snapshot()
    assert (rows(view), head(view), view.next_step.text) == (
        [("护甲", HERO_ARMOR)],
        ("", ""),
        "▶ 等建议…",
    )


@MODES
def test_hero_view_shows_the_uncertain_warning(detail):
    model = OverlayModel(name, detail=detail)
    choosing(model, hero_select_state(uncertain=True))
    assert model.snapshot().warning == "⚠ 状态可能不准"


@MODES
def test_concurrent_hero_calls_are_safe(detail):
    model = OverlayModel(name, detail=detail)
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


@MODES
def test_new_offer_drops_the_old_hero_ai_lines(detail):
    # 上一组的错误、提示不能留到新的一组上（新的请求会再给"分析中…"）
    model = OverlayModel(name, detail=detail)
    choosing(model)
    model.ai_text("AI 超时", "hero")
    heroes = ((89, "BG21_HERO_000", 10, 1), (95, "BG22_HERO_003", 12, 2))
    again = hero_select_state(heroes, seq=2)
    model.on_update(again, [offered_event(again, changed=1)])
    assert model.snapshot().notice is None
    model.ai_text("生成 AI 局面时出错，本次跳过")
    model.on_update(again.model_copy(update={"seq": 3}), [])  # 别的更新：提示留着
    assert model.snapshot().notice == "生成 AI 局面时出错，本次跳过"


@MODES
def test_title_before_the_recruit_starts_leaves_out_the_upgrade(detail):
    # TURN 变成 1 之后约 0.5 秒才有升本按钮：这时不显示"已满本"
    model = OverlayModel(name, detail=detail)
    early = make_state(turn=1, gold=0, upgrade_cost=None, main_action=False)
    model.on_update(early, [])
    assert model.snapshot().title == "第 1 回合 · 铸币 0 · 1 本"
    model.on_update(make_state(turn=1, gold=3, seq=2), started(1))
    assert model.snapshot().title == "第 1 回合 · 铸币 3 · 1 本 · 升本 5"


def test_hero_rules_error_keeps_the_hero_row():
    model = OverlayModel(name)
    choosing(model)
    model.rules_error(hero=True)
    view = model.snapshot()
    assert rows(view) == [("护甲", HERO_ARMOR)]  # 英雄和护甲照样列出，只是没有 ★
    assert view.notice is None


# ---- 下一步 ----


def step(model):
    return model.snapshot().next_step


@MODES
def test_next_step_follows_the_rules_plan_and_moves_on(detail):
    model = OverlayModel(name, detail=detail)
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


@MODES
def test_next_step_switches_to_the_ai_plan_and_then_the_deep_one(detail):
    model = OverlayModel(name, detail=detail)
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
    assert step(model).text == "▶ 买 左数第 2 个 蚀脑怪"  # 升本要 6 金、只有 5 金：深度让路
    model.on_update(T3.model_copy(update={"me": T3.me.model_copy(update={"gold": 6})}), [])
    assert step(model) == NextStep("▶ 升本 → 3 本（6 金）", Target(button="upgrade"), badge="1")


@MODES
def test_next_step_waits_for_new_ai_advice_after_a_refresh(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_ai(T3, ai_advice(), False)
    model.on_update(REFRESHED, [])
    assert step(model) == NextStep("▶ 等新的建议…", tone="muted")
    model.ai_text(BUDGET_EXHAUSTED)  # AI 不会再来：退回规则的方案
    assert step(model).text == "✓ 建议的步骤都做完了"


@MODES
def test_next_step_for_an_expired_deep_plan_uses_the_fast_one(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", expired="invalid", **DEEP), False)
    model.show_ai(T3, ai_advice(), False)
    assert step(model).text == "▶ 买 左数第 2 个 蚀脑怪"


@MODES
def test_next_step_for_each_kind_of_action(detail):
    from factory import shop_spell

    hp = power(targets=(10,))
    state = T3.model_copy(
        update={
            "shop": (*SHOP, shop_spell(22, "S", 1)),
            "me": T3.me.model_copy(update={"hero_power": hp}),
        }
    )
    model = OverlayModel(lambda c: {"S": "香蕉"}.get(c, name(c)), detail=detail)
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


@MODES
def test_next_step_is_none_in_combat_and_without_a_game(detail):
    model = OverlayModel(name, detail=detail)
    assert step(model) is None
    model.on_update(make_state(turn=3, phase=Phase.COMBAT), [])
    assert step(model) is None


@MODES
def test_next_step_at_hero_select_prefers_the_ai_pick(detail):
    model = OverlayModel(name, detail=detail)
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


def test_choice_row_and_next_step_with_ai():
    model = OverlayModel(choice_name)
    state = offered_choice(model)
    view = model.snapshot()
    assert view.title == "第 9 回合 · 铸币 3 · 1 本 · 升本 5"  # 页眉不变，"3 选 1"不进页眉
    assert rows(view) == [("黑暗之赐", "摧心招募者 · 怒胆喷毒鱼人 · 泰瑞尔")]
    assert view.next_step == NextStep("▶ 等 AI 选…", tone="muted")
    assert head(view) == ("", "分析中…")
    model.ai_pending("choice")
    assert head(model.snapshot()) == ("", "分析中…")
    model.show_choice(state, choice_advice())
    view = model.snapshot()
    assert rows(view) == [("黑暗之赐", "摧心招募者 · 怒胆喷毒鱼人 · ★泰瑞尔")]
    assert view.next_step == NextStep(
        "▶ 选 左数第 3 个 泰瑞尔",
        Target(row="choice", index=2, count=3),
        slots="选项 ○ ○ ●",
        reason="32/32 直接成主力",
        badge="选",
    )
    assert head(view) == ("AI", "")


@MODES
def test_choice_without_ai_asks_the_player_to_choose(detail):
    model = OverlayModel(choice_name, ai_enabled=False, detail=detail)
    offered_choice(model)
    view = model.snapshot()
    assert view.next_step == NextStep("▶ 自己选一个（AI 没有建议）", tone="muted")
    assert (head(view), view.notice) == (("", ""), None)
    model = OverlayModel(choice_name, choice_ai=False, detail=detail)
    offered_choice(model)
    assert step(model).text == "▶ 自己选一个（AI 没有建议）"
    model = OverlayModel(choice_name, detail=detail)
    offered_choice(model)
    model.ai_pending("choice")
    model.ai_text("AI 超时", "choice")
    view = model.snapshot()
    assert (view.notice, head(view)) == ("AI 超时", ("", ""))
    assert view.next_step.text == "▶ 自己选一个（AI 没有建议）"


@MODES
def test_choice_row_disappears_once_chosen_and_ignores_stale_advice(detail):
    model = OverlayModel(choice_name, detail=detail)
    state = offered_choice(model)
    model.show_choice(state, choice_advice(choice_id=11))  # 上一组的结果
    assert step(model).text == "▶ 等 AI 选…"
    model.show_choice(state, choice_advice())
    from factory import resolved_event

    done = make_state(turn=9, seq=2, shop=SHOP)
    model.on_update(done, [resolved_event(12, picked=6933)])
    view = model.snapshot()
    assert view.blocks == ()
    assert view.next_step == NextStep("▶ 等建议…", tone="muted")
    offered_choice(model, seq=3)  # 同一组编号再出现也不会沿用旧的推荐（已经清空）
    assert step(model).text == "▶ 等 AI 选…"


def test_choice_hides_the_recruit_lines_until_chosen():
    from factory import choice_event, choice_state, resolved_event

    model = OverlayModel(choice_name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_ai(T3, ai_advice(summary="先保血"), False)
    choice = choice_state(turn=3, shop=SHOP)
    model.on_update(choice, [choice_event(choice)])
    assert [tag for tag, _ in rows(model.snapshot())] == ["黑暗之赐"]  # 没有"然后""目标"
    model.on_update(T3.model_copy(update={"seq": 9}), [resolved_event(12, turn=3)])
    assert labeled(model.snapshot(), "目标") == "先保血"


def test_choice_before_main_action_starts_the_turn():
    model = OverlayModel(choice_name)
    model.on_update(make_state(turn=5, seq=1), started(5))
    model.show_rules(make_state(turn=5, seq=1), rules_advice(turn=5))
    offered_choice(model, turn=6, main_action=False, seq=2)
    view = model.snapshot()
    assert view.title == "第 6 回合 · 铸币 3 · 1 本"
    assert [tag for tag, _ in rows(view)] == ["黑暗之赐"]  # 新回合：上一回合的建议清空
    assert view.next_step.text == "▶ 等 AI 选…"


def test_condense_keeps_the_next_step_and_drops_the_option_names():
    model = OverlayModel(choice_name)
    state = offered_choice(model)
    model.show_choice(state, choice_advice())
    view = model.snapshot()
    one = condense(view, 1)
    assert one.next_step == view.next_step
    assert one.blocks == ()


@MODES
def test_concurrent_choice_updates_are_safe(detail):
    from factory import choice_event, choice_state, resolved_event

    model = OverlayModel(choice_name, detail=detail)
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


@MODES
def test_fast_repair_shows_repairing_and_the_rules_step_meanwhile(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.ai_pending()
    model.ai_repairing("fast", "第 1 步：铸币不够")
    view = model.snapshot()
    assert head(view) == ("规则", "· 改正中…")
    assert view.next_step.text == "▶ 买 左数第 1 个 海盗"  # 还是按规则方案
    model.show_ai(T3, ai_advice(), False)
    assert head(model.snapshot()) == ("AI", "")


@MODES
def test_deep_repair_changes_nothing(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.ai_pending()
    model.ai_pending("deep")
    model.show_ai(T3, ai_advice(), False)
    before = model.snapshot()
    model.ai_repairing("deep", "第 2 步：刷新要放在最后，刷新之后不能再有动作")
    after = model.snapshot()
    assert head(after) == ("AI", "· 分析中…")  # 还在等深度分析
    assert after.version == before.version  # 什么都没变
    model.show_ai(T3, llm_advice(BUY_20, lane="deep", **DEEP), False)
    assert model.snapshot().source == "AI 深度"


@MODES
def test_choice_repair_still_waits_for_the_ai(detail):
    model = OverlayModel(choice_name, detail=detail)
    state = offered_choice(model)
    model.ai_pending("choice")
    model.ai_repairing("choice", "推荐的选项不在选项里")
    view = model.snapshot()
    assert head(view) == ("", "改正中…")
    assert view.next_step == NextStep("▶ 等 AI 选…", tone="muted")  # 不是"自己选一个"
    model.show_choice(state, choice_advice())
    assert step(model).text == "▶ 选 左数第 3 个 泰瑞尔"


@MODES
def test_hero_repair_shows_repairing_until_the_pick_arrives(detail):
    model = OverlayModel(name, detail=detail)
    st = choosing(model)
    model.ai_pending("hero")
    model.ai_repairing("hero", "推荐的英雄不在备选里")
    assert head(model.snapshot()) == ("", "改正中…")
    model.show_hero(st, hero_advice(st))
    view = model.snapshot()
    assert (head(view), view.next_step.reason) == (("AI", ""), "0 费技能每回合白送属性")


@MODES
def test_choice_lane_notice_without_budget_lets_the_player_choose(detail):
    from lscs.llm.notices import NO_BUDGET_THIS_TIME

    model = OverlayModel(choice_name, detail=detail)
    offered_choice(model)
    model.ai_pending("choice")
    model.ai_text(NO_BUDGET_THIS_TIME, "choice")  # 调度器的分路提示（设计文档第 5.1 节）
    view = model.snapshot()
    assert view.notice == "这次不再分析：本局 AI 预算已用完"
    assert view.next_step == NextStep("▶ 自己选一个（AI 没有建议）", tone="muted")


# ---- 对手一览 ----

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


def pool_note(card_id, card_name, have, left, shop=0):
    from lscs.advisor.pool import PoolNote

    return PoolNote(card_id, card_name, have, left, shop)


def test_recruit_in_compact_mode_has_no_opponents_look_for_or_pairs():
    notes = (pool_note("C", "蚀脑怪", 2, 11),)
    model = OverlayModel(name)
    model.on_update(T10, started(10))
    model.show_rules(T10, rules_advice(turn=10).model_copy(update={"pool_notes": notes}))
    advice = llm_advice(BUY_21, turn=10, look_for=("飘荡祭品",), summary="找祭品")
    model.show_ai(T10, advice.model_copy(update={"pool_notes": notes}), False)
    view = model.snapshot()
    assert rows(view) == [("目标", "找祭品")]
    text = str(view)
    assert not any(word in text for word in ("要找", "对子", "厄祖玛特", "詹迪斯", "威胁"))


COMBAT_10 = with_rivals(make_state(turn=10, phase=Phase.COMBAT, seq=5))


def test_combat_title_and_the_strongest_line():
    model = OverlayModel(name)
    model.on_update(COMBAT_10, [Event(type=EventType.COMBAT_STARTED, turn=10)])
    view = model.snapshot()
    assert (view.title, view.compact, view.next_step) == (
        "第 10 回合 · 战斗中 · 对手 詹迪斯",
        False,
        None,
    )
    assert view.blocks == (
        Block("lines", (Line("厄祖玛特 · 畸变×1 身材 180", "dim", label="最强"),), small=True),
    )
    assert condense(view, 3) == view  # 唯一的一行总是保留


def test_combat_strongest_being_fought_is_only_the_title():
    model = OverlayModel(name)
    model.on_update(with_rivals(COMBAT_10, next_id=3), [])
    view = model.snapshot()
    assert (view.title, view.compact, view.blocks) == (
        "第 10 回合 · 战斗中 · 对手 厄祖玛特",
        True,
        (),
    )


@MODES
def test_combat_stays_one_line_when_opponents_are_off_or_unknown(detail):
    model = OverlayModel(name, opponents=False, detail=detail)
    model.on_update(COMBAT_10, [])
    view = model.snapshot()
    assert (view.title, view.compact, view.blocks) == ("第 10 回合 · 战斗中", True, ())
    model = OverlayModel(name, detail=detail)
    model.on_update(make_state(turn=3, phase=Phase.COMBAT), [])
    assert (model.snapshot().title, model.snapshot().compact) == ("第 3 回合 · 战斗中", True)


def test_combat_strongest_with_an_empty_board():
    seen_empty = {2: rival(2, 20, 0, 1, 3, (), 5), 5: RIVALS[5]}
    model = OverlayModel(name)
    model.on_update(with_rivals(COMBAT_10, next_id=5, opponents=seen_empty), [])
    assert labeled(model.snapshot(), "最强") == "洛卡拉 · 空场"


# ---- 复盘 ----


def _review(**kw):
    from dataclasses import replace
    from datetime import datetime
    from pathlib import Path

    from test_review_flags import RULES, curve_turns, record

    from lscs.review.flags import Flag
    from lscs.review.report import review_game

    review = review_game(record(curve_turns()), RULES, name, datetime(2026, 10, 11, 23, 20))
    moments = (
        Flag(
            kind="upgrade_without_board",
            turns=(7, 8),
            severity=3,
            facts="连续升本",
            fix="升本也买；补场面",
            streak_turns=(8, 9),
            streak_lost=30,
        ),
        Flag(kind="upgrade_without_board", turns=(5,), severity=2, facts="升 4 本", fix="x"),
        Flag(kind="good_turn", turns=(11,), severity=0, facts="凑出金色蚀脑怪，打出 10 血", fix=""),
    )
    values = dict(
        moments=moments,
        focus="升本的回合也要买一张随从",
        path=Path("reviews/x.md"),
        shown_path="reviews/2026-10-11_2320_凯瑞尔·罗姆_第3名.md",
    )
    values.update(kw)
    return replace(review, **values)


GAME_OVER = make_state(phase=Phase.GAME_OVER).model_copy(update={"final_place": 3})


def test_review_shows_under_the_game_over_title():
    model = OverlayModel(name)
    model.on_update(GAME_OVER, [Event(type=EventType.GAME_ENDED, turn=13, data={"place": 3})])
    assert model.snapshot().compact
    model.show_review(GAME_OVER, _review(ai_state="pending"))
    view = model.snapshot()
    assert (view.title, view.compact, view.next_step) == ("对局结束 · 第 3 名", False, None)
    # 关键时刻每条一行、下局重点最多两行；文件路径和"生成中"只在终端和详细模式里
    assert view.blocks == (
        Block(
            "lines",
            (
                Line("① 第 7、8 回合 升本不补场面 → 连掉 30 血", max_lines=1),
                Line("② 第 5 回合 升本不补场面", max_lines=1),
                Line("③ 第 11 回合 做得好：凑出金色蚀脑怪", max_lines=1),
                Line("下局重点：升本的回合也要买一张随从", role="goal", max_lines=2),
            ),
        ),
    )
    assert condense(view, 1) == view
    assert condense(view, 2).blocks[0].lines[-1].max_lines == 1


def test_review_without_moments_and_save_error():
    model = OverlayModel(name)
    model.on_update(GAME_OVER, [])
    model.show_review(GAME_OVER, _review(moments=(), save_error="没有权限", path=None))
    assert rows(model.snapshot()) == [
        ("", "这局没有触发提醒"),
        ("", "下局重点：升本的回合也要买一张随从"),
    ]


@MODES
def test_review_is_cleared_by_the_next_game_and_ignored_outside_game_over(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(GAME_OVER, [])
    model.show_review(GAME_OVER, _review())
    hero = make_state(turn=0, phase=Phase.HERO_SELECT, seq=9, hero="TB_BaconShop_HERO_PH")
    model.on_update(hero, [Event(type=EventType.GAME_STARTED, turn=0)])
    model.on_update(GAME_OVER.model_copy(update={"seq": 10}), [])
    assert model.snapshot().blocks == ()
    # 下一局已经开始了：晚到的复盘不显示
    model.on_update(T3, started(3))
    model.show_review(T3, _review())
    model.on_update(GAME_OVER.model_copy(update={"seq": 11}), [])
    assert model.snapshot().blocks == ()


# ---- 站位（站位建议设计文档第 6.2 节） ----

from lscs.advisor.models import Move, PositionAdvice  # noqa: E402

POS_NAMES = {"T": "泰瑞尔", "SP": "孢林卵怪", "SK": "无害的骨颅", "IR": "铁血防御机", "V": "虚悯者"}


def pos_name(card_id):
    return POS_NAMES.get(card_id, name(card_id))


def pos_board(*pairs):
    """[(实体编号, 卡牌 ID)] 按顺序摆在场上。"""
    return [minion(e, c, 2, 2) for e, c in pairs]


# 用户录像第 9 回合买完：泰瑞尔打在最左，孢林卵怪在第 2 个
T9_BOARD = [(1, "T"), (2, "SP"), (3, "C"), (4, "C"), (5, "A"), (6, "V")]
T9_SHOP = [shop_minion(20, "B", 3, 2)]
T9_START = make_state(
    turn=9, gold=3, tier=6, upgrade_cost=None, shop=T9_SHOP, board=pos_board(*T9_BOARD)
)
T9 = make_state(turn=9, gold=0, tier=6, upgrade_cost=None, seq=2, board=pos_board(*T9_BOARD))


def pos_move(entity_id, index, before_id=None, role="deathrattle", short="亡语最早触发"):
    return Move(
        entity_id=entity_id, index=index, before_id=before_id, role=role, reason="", short=short
    )


def pos_advice(board, *moves, turn=9):
    ids = tuple(e for e, _ in board)
    return PositionAdvice(state_seq=2, turn=turn, board=ids, target=ids, moves=tuple(moves))


def t9_model(detail=False):
    model = OverlayModel(pos_name, detail=detail)
    model.on_update(T9_START, started(9))
    plan = Plan(source="rules", actions=(Action(type="buy", entity_id=20),))
    model.show_rules(T9_START, Advice(state_seq=1, turn=9, plan=plan))
    model.on_update(T9, [])
    model.show_position(T9, pos_advice(T9_BOARD, pos_move(2, 0)))
    return model


SPAWN_STEP = NextStep(
    "▶ 把 左数第 2 个 孢林卵怪 拖到最左边",
    Target(row="board", index=1, count=6),
    slots="场面 ○ ● ○ ○ ○ ○   拖到最左边",
    reason="亡语最早触发",
    badge="拖",
    reason_label="为何",
)


@MODES
def test_position_after_the_plan_is_done(detail):
    model = t9_model(detail)
    view = model.snapshot()
    assert view.title == "第 9 回合 · 铸币 0 · 6 本 · 已满本"
    assert view.next_step == SPAWN_STEP
    if detail:  # 本回合：方案做完了；站位只有一步，已经在"现在"里，不再写一行
        assert texts(view) == ["已按建议操作完"]
    else:
        assert view.blocks == ()  # 只有一步：没有"然后"
    # 拖完：这一步自然消失，不用等运行器
    dragged = T9.model_copy(
        update={
            "seq": 3,
            "me": T9.me.model_copy(
                update={"board": tuple(pos_board((2, "SP"), (1, "T"), *T9_BOARD[2:]))}
            ),
        }
    )
    model.on_update(dragged, [])
    assert model.snapshot().next_step == NextStep("✓ 建议的步骤都做完了", tone="muted")
    model.show_position(dragged, pos_advice([(2, "SP"), (1, "T"), *T9_BOARD[2:]]))
    assert model.snapshot().next_step.text == "✓ 建议的步骤都做完了"


def test_position_waits_for_the_plan_steps_first():
    model = OverlayModel(pos_name)
    rich = T9.model_copy(update={"me": T9.me.model_copy(update={"gold": 1})})
    model.on_update(rich, started(9))
    plan = Plan(source="rules", actions=(Action(type="refresh"),))
    model.show_rules(rich, Advice(state_seq=1, turn=9, plan=plan))
    model.show_position(rich, pos_advice(T9_BOARD, pos_move(2, 0)))
    assert step(model) == NextStep("▶ 刷新商店", Target(button="refresh"), badge="1")
    assert labeled(model.snapshot(), "然后") == "站位 把 孢林卵怪 拖到最左边"
    # 铸币花在了别处（用户录像第 3 回合：方案说刷新，玩家买了孢林卵怪）：刷新做不了，
    # 运行器回合中途不再发"无需操作"，面板上的刷新留着，但不挡站位
    model.on_update(T9, [])
    assert step(model) == SPAWN_STEP
    model.show_position(T9, None)  # 没有站位了：刷新还是做不起
    assert step(model) == NextStep("无需操作", tone="muted")
    model.show_position(T9, pos_advice(T9_BOARD, pos_move(2, 0)))
    # 方案本来就没有动作（铸币 0）：直接是站位
    model.show_rules(T9, Advice(state_seq=1, turn=9, plan=Plan(source="rules")))
    assert step(model) == SPAWN_STEP


def test_then_line_with_steps_and_a_position_to_adjust():
    model = OverlayModel(pos_name)
    rich = T9_START.model_copy(update={"me": T9_START.me.model_copy(update={"gold": 9})})
    model.on_update(rich, started(9))
    plan = Plan(
        source="rules",
        actions=(Action(type="upgrade"), Action(type="refresh"), Action(type="freeze")),
    )
    model.show_rules(rich, Advice(state_seq=1, turn=9, plan=plan))
    model.show_position(rich, pos_advice(T9_BOARD, pos_move(2, 0)))
    assert labeled(model.snapshot(), "然后") == "② 刷新  ③ 冻结  · 站位 把 孢林卵怪 拖到最左边"


@MODES
def test_outdated_rules_buy_does_not_hide_the_position_step(detail):
    # CC0 第 5 回合：规则方案说买，玩家把铸币花在了别处、只剩 1 金（米尔豪斯买一张 2 金）；
    # 运行器只在现在的规则方案里没有买、卖时才给站位，所以面板上留着的买是旧方案的
    model = OverlayModel(pos_name, detail=detail)
    model.on_update(T9_START, started(9))
    plan = Plan(source="rules", actions=(Action(type="buy", entity_id=20), Action(type="refresh")))
    model.show_rules(T9_START, Advice(state_seq=1, turn=9, plan=plan))
    poor = T9_START.model_copy(update={"seq": 2, "me": T9_START.me.model_copy(update={"gold": 1})})
    model.on_update(poor, [])
    assert step(model) == NextStep("无需操作", tone="muted")  # 还没有站位：买不起的买不当下一步
    model.show_position(poor, pos_advice(T9_BOARD, pos_move(2, 0)))
    assert step(model) == SPAWN_STEP
    # AI 的买照常排在前面（规则不买的牌 AI 可能会买，比如酒馆法术），只要买得起
    ai = Plan(source="llm", actions=(Action(type="buy", entity_id=20),))
    model.show_ai(poor, Advice(state_seq=2, turn=9, plan=ai), False)
    assert step(model) == SPAWN_STEP  # 1 金买不起 3 金的随从：AI 的方案让路
    from factory import shop_spell

    spell = poor.model_copy(update={"seq": 3, "shop": (*poor.shop, shop_spell(22, "S", 1))})
    model.on_update(spell, [])
    ai = Plan(source="llm", actions=(Action(type="buy", entity_id=22),))
    model.show_ai(spell, Advice(state_seq=3, turn=9, plan=ai), False)
    assert step(model).text == "▶ 买 左数第 2 个 S（法术）"


@MODES
def test_position_does_not_jump_the_queue_while_waiting_for_advice(detail):
    model = OverlayModel(pos_name, detail=detail)
    model.on_update(T9, started(9))
    model.show_position(T9, pos_advice(T9_BOARD, pos_move(2, 0)))
    assert step(model) == NextStep("▶ 等建议…", tone="muted")  # 还没有任何方案


def test_two_position_steps_one_after_the_other():
    board = [(1, "C"), (2, "IR"), (3, "V"), (4, "SK"), (5, "C")]
    state = make_state(turn=9, gold=0, board=pos_board(*board))
    model = OverlayModel(pos_name)
    model.on_update(state, started(9))
    model.show_rules(state, Advice(state_seq=1, turn=9, plan=Plan(source="rules")))
    avenge = pos_move(2, 4, role="avenge", short="复仇放右边等触发")
    model.show_position(state, pos_advice(board, pos_move(4, 0), avenge))
    view = model.snapshot()
    assert view.next_step.text == "▶ 把 左数第 4 个 无害的骨颅 拖到最左边"
    assert rows(view) == [("然后", "② 把 铁血防御机 拖到最右边")]
    first = [board[3], board[0], board[1], board[2], board[4]]
    model.on_update(
        state.model_copy(
            update={"seq": 2, "me": state.me.model_copy(update={"board": tuple(pos_board(*first))})}
        ),
        [],
    )
    assert step(model) == NextStep(
        "▶ 把 左数第 3 个 铁血防御机 拖到最右边",
        Target(row="board", index=2, count=5),
        slots="场面 ○ ○ ● ○ ○   拖到最右边",
        reason="复仇放右边等触发",
        badge="拖",
        reason_label="为何",
    )
    assert model.snapshot().blocks == ()
    both = [board[3], board[0], board[2], board[4], board[1]]
    model.on_update(
        state.model_copy(
            update={"seq": 3, "me": state.me.model_copy(update={"board": tuple(pos_board(*both))})}
        ),
        [],
    )
    assert step(model) == NextStep("无需操作", tone="muted")


@MODES
def test_position_step_left_of_a_minion(detail):
    board = [(1, "SP"), (2, "C"), (3, "V"), (4, "SK")]
    state = make_state(turn=9, gold=0, board=pos_board(*board))
    model = OverlayModel(pos_name, detail=detail)
    model.on_update(state, started(9))
    model.show_rules(state, Advice(state_seq=1, turn=9, plan=Plan(source="rules")))
    model.show_position(state, pos_advice(board, pos_move(4, 1, before_id=2)))
    assert step(model).text == "▶ 把 左数第 4 个 无害的骨颅 拖到 蚀脑怪 的左边"
    assert step(model).slots == "场面 ○ ○ ○ ●   拖到 蚀脑怪 的左边"
    done = [board[0], board[3], board[1], board[2]]
    model.on_update(
        state.model_copy(
            update={"seq": 2, "me": state.me.model_copy(update={"board": tuple(pos_board(*done))})}
        ),
        [],
    )
    assert step(model).text == "无需操作"


@MODES
def test_position_hidden_when_the_board_has_other_minions(detail):
    model = t9_model(detail)
    bought = T9.model_copy(
        update={
            "seq": 3,
            "me": T9.me.model_copy(update={"board": tuple(pos_board(*T9_BOARD, (7, "B")))}),
        }
    )
    model.on_update(bought, [])  # 又买了一个（运行器会给新的结果）：整条不显示
    assert model.snapshot().next_step.badge != "拖"
    model.show_position(bought, None)
    assert model.snapshot().next_step.badge != "拖"


@MODES
def test_position_cleared_by_a_new_turn_and_kept_when_condensed(detail):
    model = t9_model(detail)
    assert condense(model.snapshot(), 3, detail).next_step == SPAWN_STEP
    t10 = make_state(turn=10, gold=10, tier=6, upgrade_cost=None, seq=5, board=pos_board(*T9_BOARD))
    model.on_update(t10, started(10))
    model.show_rules(t10, Advice(state_seq=5, turn=10, plan=Plan(source="rules")))
    assert step(model) == NextStep("无需操作", tone="muted")
    model.show_position(t10, None)
    assert step(model) == NextStep("无需操作", tone="muted")


@MODES
def test_position_after_a_rules_error(detail):
    model = OverlayModel(pos_name, ai_enabled=False, detail=detail)
    model.on_update(T9, started(9))
    model.rules_error()
    model.show_position(T9, pos_advice(T9_BOARD, pos_move(2, 0)))
    # AI 没开：不会再有方案，"下一步"直接是站位
    view = model.snapshot()
    assert (view.next_step, view.notice) == (SPAWN_STEP, "本回合规则建议出错")
    # AI 开着：等它的方案，站位不插队
    model = OverlayModel(pos_name, detail=detail)
    model.on_update(T9, started(9))
    model.rules_error()
    model.show_position(T9, pos_advice(T9_BOARD, pos_move(2, 0)))
    assert step(model) == NextStep("▶ 等建议…", tone="muted")
    model.ai_text(BUDGET_EXHAUSTED)  # AI 不会再来了
    view = model.snapshot()
    assert (view.next_step, view.notice) == (SPAWN_STEP, "本局 AI 次数已用完")


@MODES
def test_unknown_refresh_cost_still_blocks_the_position_step(detail):
    # 不知道刷新要多少钱（refresh_cost 为 None）时，不能说它做不了
    unknown = T9.model_copy(update={"me": T9.me.model_copy(update={"refresh_cost": None})})
    model = OverlayModel(pos_name, detail=detail)
    model.on_update(unknown, started(9))
    plan = Plan(source="rules", actions=(Action(type="refresh"),))
    model.show_rules(unknown, Advice(state_seq=1, turn=9, plan=plan))
    model.show_position(unknown, pos_advice(T9_BOARD, pos_move(2, 0)))
    assert step(model) == NextStep("▶ 刷新商店", Target(button="refresh"), badge="1")


# ---- 本版本统计（本版本统计设计文档第 6.3、7.1 节） ----

HERO_STATS_TITLE = "选英雄 · 本版本战绩 · Firestone"


def hero_stats(model, state):
    from helpers import fixture_stats

    from lscs.advisor.hero_rules import rule_hero_pick
    from lscs.advisor.stats_note import hero_note

    stats = fixture_stats()
    model.show_stats(state, hero_note(state.hero_select, stats))
    model.show_hero(state, rule_hero_pick(state.hero_select, stats))


@NEEDS_STATS_FIXTURES
def test_hero_view_with_stats():
    model = OverlayModel(name)
    st = choosing(model)
    hero_stats(model, st)
    view = model.snapshot()
    assert view.title == HERO_STATS_TITLE
    assert rows(view) == [
        (
            "名次",
            "★凯瑞尔·罗姆 3.67 · 泰瑟兰·血望者 3.99 · 洛，在世传奇 3.91 · 了不起的杰弗里斯 4.04",
        )
    ]
    assert view.next_step.text == "▶ 选 左数第 1 个 凯瑞尔·罗姆"  # ★ 是规则推荐，AI 优先
    model.show_hero(st, hero_advice(st, pick=91))
    view = model.snapshot()
    assert view.next_step.text == "▶ 选 左数第 3 个 洛，在世传奇"
    assert "★洛，在世传奇 3.91" in labeled(view, "名次")  # ★ 跟着"下一步"


@NEEDS_STATS_FIXTURES
def test_hero_without_stats_keeps_its_armor():
    heroes = ((89, "BG21_HERO_000", 10, 1), (95, "UNKNOWN_HERO", 12, 2))
    model = OverlayModel(name)
    st = choosing(model, hero_select_state(heroes))
    hero_stats(model, st)  # 有英雄查不到：规则按护甲
    view = model.snapshot()
    assert view.title == HERO_STATS_TITLE
    assert rows(view) == [("名次", "★凯瑞尔·罗姆 3.67 · UNKNOWN_HERO 护甲 12")]


@NEEDS_STATS_FIXTURES
def test_reroll_drops_the_hero_stats():
    model = OverlayModel(name)
    st = choosing(model)
    hero_stats(model, st)
    heroes = ((89, "BG21_HERO_000", 10, 1), (95, "BG22_HERO_003", 12, 2))
    again = hero_select_state(heroes, seq=2)
    model.on_update(again, [offered_event(again, changed=1)])
    view = model.snapshot()
    assert (view.title, rows(view)) == (HERO_TITLE, [("护甲", "凯瑞尔·罗姆 10 · 范达尔·雷矛 12")])
    # 晚到的上一组的条目（签名不对）不显示
    from helpers import fixture_stats

    from lscs.advisor.stats_note import hero_note

    model.show_stats(again, hero_note(st.hero_select, fixture_stats()))
    assert model.snapshot().title == HERO_TITLE


TRINKET_NAMES = {
    "BG30_Trinket_1st": "小型饰品",
    "BG35_MagicItem_890": "沉没的船锚",
    "BG36_MagicItem_402": "精神鞭笞者肖像",
    "BG30_MagicItem_703": "神秘魔方",
    "BG30_MagicItem_420": "麦迪文之书",
    "BG99_MagicItem_NONE": "没有数据的饰品",
}


def trinket_name(card_id):
    return TRINKET_NAMES.get(card_id, name(card_id))


def offered_trinkets(model, choice_id=6, seq=1, last="BG30_MagicItem_420"):
    from factory import choice_event, choice_option, choice_state, make_choice
    from helpers import fixture_stats

    from lscs.advisor.stats_note import trinket_note

    trinkets = (
        (3027, "BG35_MagicItem_890", 4),
        (3026, "BG36_MagicItem_402", 4),
        (3025, "BG30_MagicItem_703", 5),
        (3028, last, 1),
    )
    options = [
        choice_option(e, c, i, card_type="BATTLEGROUND_TRINKET", cost=cost)
        for i, (e, c, cost) in enumerate(trinkets)
    ]
    choice = make_choice(options, choice_id=choice_id, kind="trinket", source="BG30_Trinket_1st")
    state = choice_state(choice, turn=6, main_action=False, seq=seq)
    model.on_update(state, [choice_event(state)])
    model.show_stats(state, trinket_note(choice, fixture_stats()))
    model.show_choice(state, None)
    return state


TRINKET_ROW = ("小型饰品", "沉没的船锚 · ▲精神鞭笞者肖像 · 神秘魔方 · 麦迪文之书")


@NEEDS_STATS_FIXTURES
def test_trinket_choice_marks_the_best_by_stats():
    model = OverlayModel(trinket_name, ai_enabled=False)
    offered_trinkets(model)
    view = model.snapshot()
    assert rows(view) == [TRINKET_ROW]  # 统计数字只在终端和详细模式里
    # ▲ 不是推荐：AI 没开时还是让玩家自己选，不高亮
    assert view.next_step == NextStep("▶ 自己选一个（AI 没有建议）", tone="muted")


@NEEDS_STATS_FIXTURES
def test_trinket_marks_merge_when_the_ai_picks_the_best():
    from lscs.advisor.models import ChoiceAdvice

    model = OverlayModel(trinket_name)
    state = offered_trinkets(model)
    model.show_choice(state, ChoiceAdvice(choice_id=6, pick=3026, reason="稳"))
    assert "★▲精神鞭笞者肖像" in rows(model.snapshot())[0][1]
    model.show_choice(state, ChoiceAdvice(choice_id=6, pick=3025, reason="铸币够"))
    view = model.snapshot()
    assert rows(view) == [("小型饰品", "沉没的船锚 · ▲精神鞭笞者肖像 · ★神秘魔方 · 麦迪文之书")]
    assert view.next_step.text == "▶ 选 左数第 3 个 神秘魔方"  # 高亮跟 AI


@NEEDS_STATS_FIXTURES
def test_trinket_without_stats_is_listed_by_name():
    model = OverlayModel(trinket_name)
    offered_trinkets(model, last="BG99_MagicItem_NONE")
    assert rows(model.snapshot())[0][1].endswith(" · 没有数据的饰品")


@NEEDS_STATS_FIXTURES
def test_trinket_stats_cleared_with_a_new_group_or_once_chosen():
    from factory import choice_event, choice_state, resolved_event

    model = OverlayModel(trinket_name)
    offered_trinkets(model)
    other = choice_state(turn=6, seq=2)  # 换了一组（黑暗之赐）
    model.on_update(other, [choice_event(other)])
    assert rows(model.snapshot()) == [("黑暗之赐", "BG36_312 · BG33_318 · BG36_356")]
    offered_trinkets(model, seq=3)
    done = make_state(turn=6, seq=4, main_action=False)
    model.on_update(done, [resolved_event(6, turn=6, picked=3026)])
    assert model.snapshot().blocks == ()
    model.on_update(choice_state(turn=6, seq=5).model_copy(update={"choice": other.choice}), [])
    assert "▲" not in rows(model.snapshot())[0][1]


# ---- 铸币不够做第一步时换下一套方案（面板精简设计文档第 3.1 节） ----


def _with_power(state, **kw):
    return state.model_copy(update={"me": state.me.model_copy(update={"hero_power": power(**kw)})})


def test_unaffordable_ai_plans_give_way_to_the_rules_plan():
    # 用户录像第 9 回合：铸币花到 0，深度和快速的第一步都是买（3 金），规则是英雄技能（0 费）
    model = OverlayModel(name)
    broke = _with_power(T3.model_copy(update={"me": T3.me.model_copy(update={"gold": 0})}))
    model.on_update(broke, started(3))
    hp_plan = Plan(source="rules", actions=(Action(type="hero_power"),), direction="海盗")
    model.show_rules(broke, Advice(state_seq=1, turn=3, plan=hp_plan))
    model.show_ai(broke, llm_advice(BUY_21), False)
    model.show_ai(broke, llm_advice(BUY_20, lane="deep", **DEEP), False)
    view = model.snapshot()
    assert view.next_step == NextStep("▶ 用英雄技能 定罪", Target(button="hero_power"), badge="1")
    assert head(view) == ("规则", "")
    # 深度分析的思路没有过期，只是操作做不了："目标"照旧
    assert rows(view) == [("目标", "提前升 5 本找畸变核心")]
    # 铸币回来了：深度分析又排在最前面
    model.on_update(_with_power(T3.model_copy(update={"seq": 2})), [])
    assert (step(model).text, model.snapshot().source) == ("▶ 买 左数第 1 个 海盗", "AI 深度")


@MODES
def test_unaffordable_fast_plan_falls_back_to_the_rules_plan(detail):
    model = OverlayModel(name, detail=detail)
    poor = T3.model_copy(update={"me": T3.me.model_copy(update={"gold": 2})})
    model.on_update(poor, started(3))
    model.show_rules(
        poor,
        Advice(state_seq=1, turn=3, plan=Plan(source="rules", actions=(Action(type="refresh"),))),
    )
    model.show_ai(poor, ai_advice(), False)  # 买 3 金的随从，只有 2 金
    assert step(model) == NextStep("▶ 刷新商店", Target(button="refresh"), badge="1")
    assert model.snapshot().source == "规则"


@MODES
def test_upgrade_cost_decides_whether_the_first_step_is_affordable(detail):
    model = OverlayModel(name, detail=detail)
    state = make_state(turn=4, gold=5, tier=3, upgrade_cost=7, shop=SHOP)
    model.on_update(state, started(4))
    model.show_rules(state, Advice(state_seq=1, turn=4, plan=Plan(source="rules", actions=BUY_20)))
    model.show_ai(state, llm_advice((Action(type="upgrade"),), turn=4), False)
    assert step(model).text == "▶ 买 左数第 1 个 海盗"  # 升本要 7 金，只有 5 金
    unknown = state.model_copy(
        update={"seq": 2, "me": state.me.model_copy(update={"upgrade_cost": None})}
    )
    model.on_update(unknown, [])  # 不知道升本多少钱：不下结论
    assert step(model).text == "▶ 升本 → 4 本"


@MODES
def test_spells_are_priced_by_their_cost(detail):
    from factory import shop_spell

    model = OverlayModel(lambda c: {"S": "香蕉", "T": "宝藏"}.get(c, name(c)), detail=detail)
    shop = (*SHOP, shop_spell(22, "S", 1), shop_spell(23, "T", 4), shop_spell(24, "S", None))
    state = make_state(turn=3, gold=2, tier=2, upgrade_cost=6, shop=shop)
    model.on_update(state, started(3))
    model.show_rules(
        state,
        Advice(state_seq=1, turn=3, plan=Plan(source="rules", actions=(Action(type="refresh"),))),
    )
    model.show_ai(state, llm_advice((Action(type="buy", entity_id=22),)), False)
    assert step(model).text == "▶ 买 左数第 3 个 香蕉（法术）"  # 1 金的法术买得起
    model.show_ai(state, llm_advice((Action(type="buy", entity_id=23),)), False)
    assert step(model).text == "▶ 刷新商店"  # 4 金的法术买不起
    model.show_ai(state, llm_advice((Action(type="buy", entity_id=24),)), False)
    assert step(model).text == "▶ 买 左数第 5 个 香蕉（法术）"  # 没有价格：不下结论


@MODES
def test_buy_cost_follows_the_hero(detail):
    state = make_state(turn=3, gold=3, tier=2, upgrade_cost=6, shop=SHOP)
    refresh = Plan(source="rules", actions=(Action(type="refresh"),))
    texts = []
    for buy_cost in (None, lambda hero: 4 if hero == "TB_BaconShop_HERO_49" else 3):
        model = (
            OverlayModel(name, detail=detail)
            if buy_cost is None
            else OverlayModel(name, buy_cost=buy_cost, detail=detail)
        )
        model.on_update(state, started(3))
        model.show_rules(state, Advice(state_seq=1, turn=3, plan=refresh))
        model.show_ai(state, ai_advice(), False)
        texts.append(step(model).text)
    # 默认买牌 3 金：买得起；这个英雄买牌要 4 金：买不起，换成规则的刷新
    assert texts == ["▶ 买 左数第 2 个 蚀脑怪", "▶ 刷新商店"]


@MODES
def test_hero_power_cost_counts_and_the_rules_plan_is_not_skipped_for_another(detail):
    model = OverlayModel(name, detail=detail)
    state = _with_power(make_state(turn=3, gold=1, tier=2, upgrade_cost=6, shop=SHOP), cost=2)
    model.on_update(state, started(3))
    hp = Plan(source="rules", actions=(Action(type="hero_power"),))
    model.show_rules(state, Advice(state_seq=1, turn=3, plan=hp))
    # 规则的方案不让给别的方案；但它剩下的第一步做不起（技能 2 费、只有 1 金）时不当"下一步"
    view = model.snapshot()
    assert (view.next_step, view.source) == (NextStep("无需操作", tone="muted"), "规则")
    refresh = Plan(source="rules", actions=(Action(type="refresh"),))
    model.show_rules(state, Advice(state_seq=1, turn=3, plan=refresh))
    model.show_ai(state, llm_advice((Action(type="hero_power"),)), False)
    assert step(model).text == "▶ 刷新商店"  # AI 的技能 2 费，只有 1 金：换成规则的


# ---- 符号 ----


def _all_views(detail=False):
    """各种状态下的面板（招募、站位、选项、选英雄、战斗、复盘）。"""
    views = []
    model = OverlayModel(name, detail=detail)
    model.on_update(T10, started(10))
    model.show_rules(T10, rules_advice(turn=10))
    views.append(model.snapshot())
    model.show_ai(T10, llm_advice(BUY_21, turn=10, **DEEP), False)
    views.append(model.snapshot())
    model.on_update(COMBAT_10, [])
    views.append(model.snapshot())
    views.append(t9_model(detail).snapshot())
    hero = OverlayModel(name, detail=detail)
    st = choosing(hero)
    hero.show_hero(st, rules_pick(st))
    hero.show_hero(st, hero_advice(st, pick=91, comments=COMMENTS))
    views.append(hero.snapshot())
    choice = OverlayModel(choice_name, detail=detail)
    cs = offered_choice(choice)
    choice.show_choice(cs, choice_advice())
    views.append(choice.snapshot())
    over = OverlayModel(name, detail=detail)
    over.on_update(GAME_OVER, [])
    over.show_review(GAME_OVER, _review())
    views.append(over.snapshot())
    return views


@MODES
def test_symbols_mean_one_thing_each(detail):
    # ▶ 只在"下一步"；★ 只标推荐的那个（每个视图至多一个）；不再有 ▶★
    for view in _all_views(detail):
        texts = [view.title, view.source, view.source_note]
        texts += [line.text for block in view.blocks for line in block.lines]
        texts += [line.label for block in view.blocks for line in block.lines]
        assert not any("▶" in text for text in texts), view
        assert sum(text.count("★") for text in texts) <= 1, view
        assert "▶★" not in str(view)


# ---- 详细模式（[overlay] detail = true，面板精简设计文档第 5 节） ----


def detailed(namer=name, **kw):
    return OverlayModel(namer, detail=True, **kw)


def block(view, kind):
    return next((b for b in view.blocks if b.kind == kind), None)


def test_detail_recruit_lists_each_step_the_goal_and_what_the_rules_say_differently():
    model = detailed()
    ready = _with_power(T3)
    model.on_update(ready, started(3))
    rules = Plan(
        source="rules",
        actions=(
            Action(type="buy", entity_id=20),
            Action(type="hero_power"),
            Action(type="refresh"),
        ),
        direction="海盗",
    )
    model.show_rules(ready, Advice(state_seq=1, turn=3, plan=rules))
    model.show_ai(ready, llm_advice((*BUY_21, Action(type="refresh")), lane="deep", **DEEP), False)
    view = model.snapshot()
    assert head(view) == ("AI 深度", "")
    assert view.blocks == (
        Block(
            "plan",
            (
                Line("① 买 蚀脑怪 3/4", "text", "step"),  # 完整写法；"下一步"那一步是正文色
                Line("② 刷新", "dim", "step"),
                Line("目标：提前升 5 本找畸变核心", role="goal"),
                # 只列规则有、AI 没有的步（短写）；刷新两边都有，不列
                Line("规则另议：买 海盗 · 用技能 定罪", "muted", "alt", max_lines=1),
            ),
            label="本回合",
        ),
        Block(
            "aside",
            (Line("要找 神明、祭品", "dim", "aside", max_lines=2),),
            label="留意",
            small=True,
        ),
    )


def test_detail_rules_plan_has_no_alternative_and_shows_its_status():
    model = detailed()
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    assert block(model.snapshot(), "plan").lines == (
        Line("① 买 海盗 3/2", "text", "step"),
        Line("② 刷新", "dim", "step"),
        Line("方向：海盗", role="goal"),  # 没有 AI 的方案：规则的方向
    )
    model.on_update(BOUGHT, [])
    assert texts(model.snapshot()) == ["② 刷新", "方向：海盗"]
    model.on_update(REFRESHED, [])  # 商店换了一批：不能说成"已按建议操作完"
    assert block(model.snapshot(), "plan").lines[0] == Line("商店已刷新", "muted", "step")
    plan = Plan(source="rules", actions=(Action(type="buy", entity_id=20),))
    model.show_rules(T3, Advice(state_seq=1, turn=3, plan=plan))
    model.on_update(BOUGHT.model_copy(update={"seq": 9}), [])  # 商店里还剩 #21，没有刷新
    assert texts(model.snapshot()) == ["已按建议操作完"]
    model.show_rules(T3, Advice(state_seq=1, turn=3, plan=Plan(source="rules")))
    assert texts(model.snapshot()) == ["无需操作"]


def test_detail_many_steps_are_cut_after_five():
    model = detailed()
    model.on_update(T3, started(3))
    steps = tuple(Action(type="refresh") for _ in range(6))
    model.show_ai(T3, ai_advice(actions=steps), False)
    assert texts(model.snapshot()) == ["① 刷新", "② 刷新", "③ 刷新", "④ 刷新", "⑤ 刷新", "…"]


def test_detail_spent_gold_shows_the_rules_step_the_position_and_the_goal():
    # 用户录像第 9 回合（recruit9_position）：铸币 0，深度分析的买做不起，换成规则的英雄技能
    state = _with_power(
        T9_START.model_copy(update={"me": T9_START.me.model_copy(update={"gold": 0})})
    )
    model = detailed(pos_name)
    model.on_update(state, started(9))
    model.show_rules(
        state,
        Advice(
            state_seq=1, turn=9, plan=Plan(source="rules", actions=(Action(type="hero_power"),))
        ),
    )
    model.show_ai(state, llm_advice(BUY_20, turn=9, lane="deep", **DEEP), False)
    model.show_position(state, pos_advice(T9_BOARD, pos_move(2, 0)))
    view = model.snapshot()
    assert (view.next_step.text, head(view)) == ("▶ 用英雄技能 定罪", ("规则", ""))
    assert block(view, "plan").lines == (
        Line("① 使用英雄技能 定罪", "text", "step"),
        Line("站位：把 孢林卵怪 拖到最左边 · 亡语最早触发", "dim"),
        Line("目标：提前升 5 本找畸变核心", role="goal"),
    )  # 显示的是规则：没有"规则另议"


def test_detail_position_line_lists_the_moves_after_the_current_one():
    board = [(1, "C"), (2, "IR"), (3, "V"), (4, "SK"), (5, "C")]
    state = make_state(turn=9, gold=0, board=pos_board(*board))
    model = detailed(pos_name)
    model.on_update(state, started(9))
    model.show_rules(state, Advice(state_seq=1, turn=9, plan=Plan(source="rules")))
    avenge = pos_move(2, 4, role="avenge", short="复仇放右边等触发")
    model.show_position(state, pos_advice(board, pos_move(4, 0), avenge))
    assert texts(model.snapshot()) == [
        "无需操作",
        "站位：② 把 铁血防御机 拖到最右边 · 复仇放右边等触发",
    ]


def test_detail_watch_block_look_for_pairs_and_opponents():
    notes = (
        pool_note("C", "蚀脑怪", 2, 11),
        pool_note("B", "海盗", 1, 14, shop=1),
        pool_note("W", "飘荡祭品", 0, 11),
    )
    model = detailed()
    model.on_update(T10, started(10))
    model.show_rules(T10, rules_advice(turn=10).model_copy(update={"pool_notes": notes}))
    advice = llm_advice(BUY_21, turn=10, look_for=("飘荡祭品", "深渊使者"), summary="找祭品")
    model.show_ai(T10, advice.model_copy(update={"pool_notes": notes}), False)
    watch = block(model.snapshot(), "aside")
    assert (watch.label, watch.small) == ("留意", True)
    assert [line.text for line in watch.lines] == [
        "要找 飘荡祭品（约 11 张）、深渊使者 · 对子 蚀脑怪×2 还剩约 11 张；海盗×1 商店有 还剩约 14 张",
        "下个对手 詹迪斯 残影 · 2 个 身材 22 · 最强 厄祖玛特 畸变×1 身材 180",
    ]
    model.on_update(with_rivals(T10, next_id=4).model_copy(update={"seq": 2}), [])
    assert texts(model.snapshot())[-1] == "下个对手 布莱恩 还没见过 · 最强 厄祖玛特 畸变×1 身材 180"
    model.on_update(with_rivals(T10, next_id=3).model_copy(update={"seq": 3}), [])
    assert texts(model.snapshot())[-1] == "下个对手 厄祖玛特 畸变×1 身材 180 · 下个对手也是最强"
    off = detailed(opponents=False)
    off.on_update(T10, started(10))
    assert block(off.snapshot(), "aside") is None  # 没有要找、对子，也不写对手


def test_detail_empty_board_on_the_watch_line():
    seen_empty = {2: rival(2, 20, 0, 1, 3, (), 5), 5: rival(5, 0, 0, 8, 2, (), 4)}
    model = detailed()
    model.on_update(with_rivals(T10, next_id=5, opponents=seen_empty), started(10))
    assert texts(model.snapshot()) == ["下个对手 詹迪斯 残影 · 空场 · 最强 洛卡拉 空场"]


def test_detail_look_for_is_cut_between_cards():
    notes = (
        pool_note("W", "飘荡祭品", 0, 11),
        pool_note("C", "蚀脑怪", 0, 11),
        pool_note("D", "深渊使者", 0, 12),
        pool_note("M", "残忍的精神鞭笞者", 0, 12),
    )
    model = detailed()
    model.on_update(T3, started(3))
    look_for = ("飘荡祭品", "蚀脑怪", "深渊使者", "残忍的精神鞭笞者")
    advice = llm_advice(BUY_21, look_for=look_for).model_copy(update={"pool_notes": notes})
    model.show_ai(T3, advice, False)
    assert texts(model.snapshot())[-1] == (
        "要找 飘荡祭品（约 11 张）、蚀脑怪（约 11 张）、深渊使者（约 12 张）…"
    )
    model.show_ai(T3, llm_advice(BUY_21, look_for=("长" * 50,)), False)
    assert texts(model.snapshot())[-1] == "要找 " + "长" * 37 + "…"  # 一张就放不下：按字截


def test_detail_choice_block():
    model = detailed(choice_name)
    state = offered_choice(model)
    view = model.snapshot()
    assert view.blocks == (
        Block(
            "choice",
            (
                Line("摧心招募者", "dim", "stats"),
                Line("怒胆喷毒鱼人", "dim", "stats"),
                Line("泰瑞尔", "dim", "stats"),
            ),
            label="选择",
            note="黑暗之赐 · 3 选 1",
        ),
    )
    model.show_choice(state, choice_advice())
    view = model.snapshot()
    assert block(view, "choice").lines[2] == Line("★ 泰瑞尔", "text", "stats")
    assert view.next_step.reason == "32/32 直接成主力"


def test_detail_choice_keeps_only_the_opponents_to_watch():
    from factory import make_choice

    model = detailed()
    model.on_update(T10, started(10))
    model.show_ai(T10, llm_advice(BUY_21, turn=10, **DEEP), False)
    model.on_update(T10.model_copy(update={"choice": make_choice(), "seq": 2}), [])
    view = model.snapshot()
    assert [b.kind for b in view.blocks] == ["choice", "aside"]  # 选项期间没有"本回合"
    assert texts(view)[-1].startswith("下个对手 詹迪斯")
    assert not any("要找" in text for text in texts(view))


@NEEDS_STATS_FIXTURES
def test_detail_trinket_stats_in_the_right_column():
    from lscs.advisor.models import ChoiceAdvice

    model = detailed(trinket_name)
    state = offered_trinkets(model)
    model.show_choice(state, ChoiceAdvice(choice_id=6, pick=3026, reason="稳"))
    choose = block(model.snapshot(), "choice")
    assert choose.note == "小型饰品 · 4 选 1 · Firestone"
    assert choose.lines == (
        Line("沉没的船锚", "dim", "stats", right="第 4.06 名 · 选 15%"),
        Line("★▲ 精神鞭笞者肖像", "text", "stats", right="第 3.89 名 · 选 27%"),
        Line("神秘魔方", "dim", "stats", right="第 3.95 名 · 选 30%"),
        Line("麦迪文之书", "dim", "stats", right="第 3.95 名 · 选 18%"),
    )


def test_detail_hero_candidates_comments_and_the_rules_pick():
    model = detailed()
    st = choosing(model)
    model.show_hero(st, rules_pick(st))
    armors = ("护甲 10", "护甲 18", "护甲 19", "护甲 17")
    names = ("凯瑞尔·罗姆", "泰瑟兰·血望者", "洛，在世传奇", "了不起的杰弗里斯")
    assert model.snapshot().blocks == (
        Block(
            "heroes",
            tuple(
                Line(f"★ {n}" if i == 0 else n, "text" if i == 0 else "dim", "stats", right=a)
                for i, (n, a) in enumerate(zip(names, armors, strict=True))
            ),
            label="备选",
        ),
    )
    model.show_hero(st, hero_advice(st, pick=90, comments=COMMENTS))
    view = model.snapshot()
    heroes, comments = view.blocks
    assert heroes.lines[1] == Line("★ 泰瑟兰·血望者", "text", "stats", right="护甲 18")
    assert heroes.lines[-1] == Line("规则另选：凯瑞尔·罗姆", "muted", "alt", max_lines=1)
    assert comments == Block(
        "comments",
        (
            Line("泰瑟兰·血望者：要靠对子才强，前期慢", "dim", "comment"),
            Line("洛，在世传奇：技能复杂，新手难发挥", "dim", "comment"),
            Line("了不起的杰弗里斯：发现随机，看运气", "dim", "comment"),
        ),
        label="点评",
        small=True,
    )
    model.show_hero(st, hero_advice(st, pick=89))  # AI 和规则选的是同一个：不另说
    assert not any(text.startswith("规则另选") for text in texts(model.snapshot()))


@NEEDS_STATS_FIXTURES
def test_detail_hero_stats_in_the_right_column():
    heroes = ((89, "BG21_HERO_000", 10, 1), (95, "UNKNOWN_HERO", 12, 2))
    model = detailed()
    st = choosing(model, hero_select_state(heroes))
    hero_stats(model, st)
    candidates = block(model.snapshot(), "heroes")
    assert candidates.note == "Firestone"
    assert [(line.text, line.right) for line in candidates.lines] == [
        ("★ 凯瑞尔·罗姆", "第 3.67 名 · 选 28% · 第 2/10"),
        ("UNKNOWN_HERO", "护甲 12"),
    ]


def test_detail_combat_lists_every_opponent():
    model = detailed()
    model.on_update(COMBAT_10, [Event(type=EventType.COMBAT_STARTED, turn=10)])
    view = model.snapshot()
    assert (view.title, view.compact, view.next_step) == (
        "第 10 回合 · 战斗中 · 对手 詹迪斯",
        False,
        None,
    )
    assert view.blocks == (
        Block(
            "opponents",
            (
                Line("对手 第8名 詹迪斯 已淘汰（残影）· 第 4 回合见 2 个 · 身材 22"),
                Line("最强 第3名 厄祖玛特 30+3 · 5本 · 畸变×1 身材 180 · 威胁 93"),
                Line(
                    "第1名 洛卡拉 30+9 · 5本 · 畸变×2 身材 38 · 第 9 回合见",
                    "dim",
                    "opponent",
                    small=True,
                ),
                Line("第2名 布莱恩 28+0 · 4本 · 还没见过阵容", "dim", "opponent", small=True),
                Line("已淘汰：范克里夫", "muted", "opponent", small=True),
            ),
            label="对手",
        ),
    )
    two = condense(view, 2, detail=True)
    assert two.blocks[0].lines == view.blocks[0].lines[:2]  # 正在打的和最强总是保留


def test_detail_combat_current_strongest_and_a_board_captured_this_turn():
    model = detailed()
    model.on_update(with_rivals(COMBAT_10, next_id=3), [])
    lines = model.snapshot().blocks[0].lines
    assert lines[0] == Line(
        "对手·最强 第3名 厄祖玛特 30+3 · 5本 · 畸变×1 身材 180 · 第 8 回合见 · 威胁 93"
    )
    assert not any(line.text.startswith("最强") for line in lines)
    seen = dict(RIVALS)
    seen[5] = rival(5, 0, 0, 8, 2, GHOST_BOARD[:1], 10)  # 第一次攻击时截到本回合的阵容
    model.on_update(with_rivals(COMBAT_10, opponents=seen).model_copy(update={"seq": 6}), [])
    first = model.snapshot().blocks[0].lines[0]
    assert first == Line("对手 第8名 詹迪斯 已淘汰（残影）· 本回合见 1 个 · 身材 14")


def test_detail_review_keeps_the_path_and_the_pending_line():
    model = detailed()
    model.on_update(GAME_OVER, [])
    model.show_review(GAME_OVER, _review(ai_state="pending"))
    (review,) = model.snapshot().blocks
    assert (review.kind, review.label) == ("review", "复盘")
    assert [line.text for line in review.lines][-2:] == [
        "已保存 reviews/2026-10-11_2320_凯瑞尔·罗姆_第3名.md",
        "AI 复盘生成中…",
    ]


def test_condense_levels_in_detail_mode():
    model = detailed()
    ready = _with_power(T10)
    model.on_update(ready, started(10))
    rules = Plan(source="rules", actions=(Action(type="hero_power"),))
    model.show_rules(ready, Advice(state_seq=1, turn=10, plan=rules))
    model.show_ai(ready, llm_advice(BUY_21, turn=10, **DEEP), False)
    view = model.snapshot()
    assert [b.kind for b in view.blocks] == ["plan", "aside"]
    assert condense(view, 0, detail=True) == view
    one = condense(view, 1, detail=True)
    assert [b.kind for b in one.blocks] == ["plan"]  # 先去掉"留意"
    assert one.blocks[0] == view.blocks[0]
    two = condense(view, 2, detail=True)
    assert [line.role for line in two.blocks[0].lines] == ["step", "goal"]  # 再去掉"规则另议"
    assert two.blocks[0].lines[-1].max_lines == 0
    three = condense(view, 3, detail=True)
    assert three.blocks[0].lines[-1].max_lines == 1  # 目标只留一行
    assert [reason_lines(level, detail=True) for level in range(4)] == [2, 2, 2, 1]
    # 选英雄：先去掉点评，再去掉右列和"规则另选"
    hero = detailed()
    st = choosing(hero)
    hero.show_hero(st, rules_pick(st))
    hero.show_hero(st, hero_advice(st, pick=90, comments=COMMENTS))
    view = hero.snapshot()
    assert [b.kind for b in condense(view, 1, detail=True).blocks] == ["heroes"]
    heroes = condense(view, 2, detail=True).blocks[0]
    assert len(heroes.lines) == 4 and all(line.right == "" for line in heroes.lines)


# ---- 战斗结果的横幅：面板模型只把它放进快照（规则见 test_overlay_banner.py）----


def combat_result(turn=10, **overrides):
    from lscs.state.models import CombatResult

    values = dict(
        turn=turn,
        opponent_id=2,
        opponent_hero="H2",
        outcome="win",
        ghost=False,
        lost=0,
        dealt=5,
        my_health=30,
        my_armor=6,
        opponent_health=30,
        opponent_armor=9,
        me_dead=False,
        opponent_dead=False,
    )
    values.update(overrides)
    return CombatResult(**values)


def settled(result):
    return [Event(type=EventType.COMBAT_RESULT, turn=result.turn)]


@MODES
def test_banner_rides_along_and_the_combat_view_does_not_change(detail):
    from dataclasses import replace

    from lscs.overlay.banner import Banner

    model = OverlayModel(name, detail=detail)
    model.on_update(COMBAT_10, [Event(type=EventType.COMBAT_STARTED, turn=10)])
    before = model.snapshot()
    assert before.banner is None  # 结算之前没有横幅
    result = combat_result()
    model.on_update(COMBAT_10.model_copy(update={"last_combat": result, "seq": 6}), settled(result))
    after = model.snapshot()
    assert after.banner == Banner("本场已定：赢 · 洛卡拉掉 5 血", "win")
    assert after.version == before.version + 1
    assert replace(after, banner=None, version=0) == replace(before, version=0)  # 面板一字不变
    assert after.title == "第 10 回合 · 战斗中 · 对手 詹迪斯"
    # 战斗中的面板（两种模式）从不写结果
    assert not any("赢" in t or "掉 5 血" in t for t in [after.title, *texts(after)])
    for level in range(4):
        assert condense(after, level, detail).banner == after.banner


def test_banner_switch_and_clock_reach_the_banner():
    result = combat_result()
    off = OverlayModel(name, combat_result=False)
    off.on_update(COMBAT_10.model_copy(update={"last_combat": result}), settled(result))
    assert off.snapshot().banner is None
    now = [100.0]
    model = OverlayModel(name, clock=lambda: now[0])
    dead = combat_result(
        turn=13, outcome="loss", lost=18, dealt=0, my_health=-14, my_armor=0, me_dead=True
    )
    over = make_state(turn=13, phase=Phase.GAME_OVER).model_copy(
        update={"last_combat": dead, "final_place": 3}
    )
    model.on_update(over, [*settled(dead), Event(type=EventType.GAME_ENDED, turn=13)])
    view = model.snapshot()
    assert view.title == "对局结束 · 第 3 名" and view.banner.expires_at == 120.0
    now[0] = 120.0
    model.on_update(over.model_copy(update={"seq": 2}), [])
    assert model.snapshot().banner is None


@MODES
def test_stale_rules_buy_at_zero_gold_is_not_the_next_step(detail):
    # 运行器回合中途不发"无需操作"：铸币花在了别处、又没有站位要调时，规则方案留着的"买"
    # 做不了，不能当"下一步"（也不框商店里的牌）；AI 的"目标"照旧
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_ai(T3, ai_advice(summary="先保血"), False)
    broke = T3.model_copy(update={"seq": 2, "me": T3.me.model_copy(update={"gold": 0})})
    model.on_update(broke, [])
    view = model.snapshot()
    assert view.next_step == NextStep("无需操作", tone="muted")
    assert view.source == "规则"
    if detail:  # 本回合不再列做不了的步，也没有"规则另议"
        assert texts(view) == ["目标：先保血"]
    else:
        assert rows(view) == [("目标", "先保血")]
    # 铸币又够了（比如卖了随从）：照常是规则的第一步
    model.on_update(T3.model_copy(update={"seq": 3}), [])
    assert step(model).text == "▶ 买 左数第 2 个 蚀脑怪"  # AI 的方案又做得起了


def test_detail_outdated_rules_steps_are_dropped_when_the_position_takes_over():
    # 规则方案里的买已经做不了（铸币花在了别处），站位成了"下一步"：本回合不再列旧的步
    model = detailed(pos_name)
    model.on_update(T9_START, started(9))
    plan = Plan(source="rules", actions=(Action(type="buy", entity_id=20), Action(type="refresh")))
    model.show_rules(T9_START, Advice(state_seq=1, turn=9, plan=plan))
    poor = T9_START.model_copy(update={"seq": 2, "me": T9_START.me.model_copy(update={"gold": 1})})
    model.on_update(poor, [])
    model.show_position(poor, pos_advice(T9_BOARD, pos_move(2, 0)))
    view = model.snapshot()
    assert view.next_step == SPAWN_STEP
    assert block(view, "plan") is None  # 没有旧的"① 买 海盗""② 刷新"


def test_first_step_unaffordable_without_enough_information():
    from lscs.overlay.model import _first_step_unaffordable

    buy = Plan(source="rules", actions=(Action(type="buy", entity_id=99),))
    broke = T3.model_copy(update={"me": T3.me.model_copy(update={"gold": 0})})
    assert not _first_step_unaffordable(buy, broke, 3)  # 要买的牌已经不在商店里：不下结论
    assert not _first_step_unaffordable(
        rules_advice().plan, broke.model_copy(update={"me": None}), 3
    )
    assert _first_step_unaffordable(rules_advice().plan, broke, 3)  # 对照：在商店里、0 金
    assert not _first_step_unaffordable(Plan(source="rules"), broke, 3)  # 没有步


@MODES
def test_badge_and_then_numbers_follow_the_full_plan_after_a_step_is_done(detail):
    model = OverlayModel(name, detail=detail)
    rich = T3.model_copy(update={"me": T3.me.model_copy(update={"gold": 9})})
    model.on_update(rich, started(3))
    plan = Plan(
        source="rules",
        actions=(
            Action(type="buy", entity_id=20),
            Action(type="buy", entity_id=21),
            Action(type="refresh"),
        ),
    )
    model.show_rules(rich, Advice(state_seq=1, turn=3, plan=plan))
    bought = BOUGHT.model_copy(update={"me": BOUGHT.me.model_copy(update={"gold": 6})})
    model.on_update(bought, [])  # 第一步做完了：编号还是完整方案里的序号
    view = model.snapshot()
    assert (view.next_step.text, view.next_step.badge) == ("▶ 买 左数第 1 个 蚀脑怪", "2")
    if detail:
        assert texts(view)[:2] == ["② 买 蚀脑怪 3/4", "③ 刷新"]
    else:
        assert labeled(view, "然后") == "③ 刷新"


# ---- 整局计划（整局计划与流派指南设计文档第 8.2 节） ----


def game_plan(**fields):
    from lscs.advisor.models import GamePlan

    values = {
        "comp": "畸变神明",
        "tribe": "ABERRATION",
        "key_cards": ("蚀脑怪", "小鬼"),
        "tiers": "第 8 回合 5 本",
        "roll": "血量低于 15 先补身材",
        "stage": "保血",
        "pivot": "第 8 回合没有核心卡就转海盗",
        "turn": 3,
    }
    values.update(fields)
    return GamePlan(**values)


PLAN_LINE = Line("畸变神明 · 保血 · 核心 蚀脑怪、小鬼✓", "dim", "aside", label="计划", max_lines=1)


def test_compact_plan_line_comes_after_the_goal():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_ai(T3, ai_advice(summary="先保血"), agrees=False)
    model.show_plan(T3, game_plan(), None)
    assert model.snapshot().blocks == (
        Block("lines", (Line("先保血", role="goal", label="目标", max_lines=2), PLAN_LINE)),
    )


def test_compact_plan_line_with_the_rules_plan_and_an_undecided_plan():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_plan(T3, game_plan(), None)
    assert [tag for tag, _ in rows(model.snapshot())] == ["然后", "方向", "计划"]
    undecided = game_plan(comp="未定", key_cards=("蚀脑怪",), stage="前期")
    model.show_plan(T3, undecided, game_plan())
    assert labeled(model.snapshot(), "计划") == "未定 · 前期 · 想找 蚀脑怪"
    bare = game_plan(key_cards=(), stage=None)
    model.show_plan(T3, bare, undecided)
    assert labeled(model.snapshot(), "计划") == "畸变神明"


def test_plan_line_goes_with_the_then_line_when_condensed():
    model = OverlayModel(name)
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_plan(T3, game_plan(), None)
    assert [tag for tag, _ in rows(condense(model.snapshot(), 1))] == ["方向"]


@MODES
def test_plan_stays_for_the_next_turns_and_goes_with_a_new_game(detail):
    model = OverlayModel(name, detail=detail)
    model.on_update(T3, started(3))
    model.show_plan(T3, game_plan(), None)
    t4 = make_state(turn=4, gold=6, seq=2, board=[minion(10, "A", 1, 1)])
    model.on_update(t4, started(4))
    assert any("畸变神明" in text for text in texts(model.snapshot()))
    model.on_update(make_state(turn=1, seq=3), [Event(type=EventType.GAME_STARTED, turn=0)])
    model.on_update(make_state(turn=1, seq=4), started(1))
    assert not any("畸变神明" in text for text in texts(model.snapshot()))


@MODES
def test_no_plan_line_while_choosing_or_in_combat(detail):
    from factory import choice_event, choice_state

    model = OverlayModel(choice_name, detail=detail)
    model.on_update(T3, started(3))
    model.show_plan(T3, game_plan(), None)
    choice = choice_state(turn=3, shop=SHOP)
    model.on_update(choice, [choice_event(choice)])
    assert not any("畸变神明" in text for text in texts(model.snapshot()))
    combat = make_state(turn=3, phase=Phase.COMBAT, seq=9)
    model.on_update(combat, [Event(type=EventType.COMBAT_STARTED, turn=3)])
    assert not any("畸变神明" in text for text in texts(model.snapshot()))


NOTE = "商店里有核心卡 蚀脑怪，方案却没买它"


def test_detail_plan_line_and_the_reminder_before_the_rules_alternative():
    model = detailed()
    model.on_update(T3, started(3))
    model.show_rules(T3, rules_advice())
    model.show_plan(T3, game_plan(key_cards=("蚀脑怪",)), None)
    refresh = llm_advice((Action(type="refresh"),), summary="刷新找核心")
    model.show_ai(T3, refresh.model_copy(update={"plan_note": NOTE}), False)
    view = model.snapshot()
    assert block(view, "plan").lines == (
        Line("① 刷新", "text", "step"),
        Line("目标：刷新找核心", role="goal"),
        Line("计划：畸变神明（保血） · 核心 蚀脑怪 · 第 8 回合 5 本", role="goal", max_lines=2),
        Line(f"提醒：{NOTE}", "muted", "alt"),
        Line("规则另议：买 海盗", "muted", "alt", max_lines=1),
    )
    texts_two = [line.text for line in block(condense(view, 2, detail=True), "plan").lines]
    assert texts_two == ["① 刷新", "目标：刷新找核心", view.blocks[0].lines[2].text]
    # 买了那张核心卡：不再提醒，计划里的它打 ✓
    bought = T3.model_copy(
        update={
            "seq": 2,
            "shop": SHOP[:1],
            "me": T3.me.model_copy(
                update={"gold": 2, "board": (*T3.me.board, minion(11, "C", 3, 4))}
            ),
        }
    )
    model.on_update(bought, [])
    lines_now = [line.text for line in block(model.snapshot(), "plan").lines]
    assert "计划：畸变神明（保血） · 核心 蚀脑怪✓ · 第 8 回合 5 本" in lines_now
    assert not any(text.startswith("提醒：") for text in lines_now)


def test_detail_without_a_plan_has_no_plan_or_reminder_lines():
    model = detailed()
    model.on_update(T3, started(3))
    advice = llm_advice((Action(type="refresh"),), summary="刷新")
    model.show_ai(T3, advice.model_copy(update={"plan_note": NOTE}), False)
    assert [line.text for line in block(model.snapshot(), "plan").lines] == ["① 刷新", "目标：刷新"]
