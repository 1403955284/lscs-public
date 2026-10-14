from pathlib import Path

from factory import make_state, shop_minion
from helpers import NEEDS_STATS_FIXTURES, TEST_POOL

from lscs.advisor.display import AdviceDisplay
from lscs.advisor.rules_config import load_rules_config
from lscs.advisor.runner import AdviceRunner
from lscs.pipeline import Pipeline
from lscs.state.models import Event, EventType, Phase
from lscs.state.tracker import StateTracker

RULES = load_rules_config(
    Path(__file__).resolve().parents[1] / "strategy" / "current" / "rules.toml"
)


def run_with_runner(lines, runner):
    pipeline = Pipeline(StateTracker(TEST_POOL), runner.on_update)
    for line in lines:
        pipeline.push(line)
    pipeline.flush()


def test_rules_advice_printed_during_recruit(mini_lines):
    out = []
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append))
    run_with_runner(mini_lines, runner)
    assert out == [
        "  📋 规则：护甲信息不全，等 AI 建议",  # 选英雄：这份合成日志的英雄没有护甲标签
        # 每回合开始先列对手一览
        "  👥 对手（主打：未知 2）",
        "  ▶★ 第 2 名 TB_BaconShop_HERO_75 30+0 · 1 本 · 还没见过阵容 · 威胁 23",
        "     第 3 名 BG22_HERO_201 30+0 · 1 本 · 还没见过阵容 · 威胁 23",
        "  📋 规则：① 买 BGS_116 1/4（商店里身材最好，有嘲讽）",
        "          ② 刷新（剩 1 金，刷新找更好的牌）",  # 米尔豪斯买牌只要 2 金
        "  👥 对手（主打：元素 1 家、未知 1）",
        "   ★ 第 2 名 TB_BaconShop_HERO_75 30+0 · 1 本 · 元素×1 身材 9"
        " · BG21_029_G(金)、BGS_116 · 第 1 回合见 · 威胁 78",
        "   ▶ 第 3 名 BG22_HERO_201 30+0 · 1 本 · 还没见过阵容 · 威胁 23",
        "  📋 规则：① 升本 → 2 本（第 2 回合按节奏该升 2 本了）   方向：海盗",
    ]
    assert runner.last_rules.turn == 2


def test_rules_error_does_not_stop(mini_lines, monkeypatch):
    import lscs.advisor.runner

    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(lscs.advisor.runner, "rule_plans", boom)
    out = []
    spy = SpyScheduler()
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append), spy)
    run_with_runner(mini_lines, runner)
    # 每回合只提示一次（第 1、2 回合）；选英雄的规则、对手一览不受影响
    rules = [line for line in out if line.startswith("  📋")]
    assert rules == ["  📋 规则：护甲信息不全，等 AI 建议"] + ["  📋 本回合规则建议出错"] * 2
    assert sum(line.startswith("  👥 对手") for line in out) == 2
    # 规则出错时照样把局面交给 AI 调度器（招募阶段的更新都带 ctx）
    assert sum(has_ctx for _, has_ctx in spy.observed) >= 2


class SpyScheduler:
    calls = input_tokens = output_tokens = 0

    def __init__(self):
        self.observed = []
        self.phases = []  # (阶段, 有没有 ctx)

    def reset_game(self):
        pass

    def observe(self, state, events, ctx):
        self.observed.append((state.seq, ctx is not None))
        self.phases.append((state.phase.value, ctx is not None))

    def tick(self):
        return []


def _events(*types, turn=2):
    return [Event(type=t, turn=turn) for t in types]


def test_no_advice_before_main_action():
    out = []
    spy = SpyScheduler()
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append), spy)
    shop = [shop_minion(20, "BGS_061", 3, 1)]
    early = make_state(turn=2, gold=4, shop=(), main_action=False)  # 铸币先到，商店还是空的
    runner.on_update(early, _events(EventType.GOLD_CHANGED))
    filled = make_state(turn=2, gold=4, shop=shop, seq=2, main_action=False)
    runner.on_update(filled, _events(EventType.SHOP_CHANGED))
    assert out == [] and spy.observed == []
    started = make_state(turn=2, gold=4, shop=shop, seq=3)
    runner.on_update(started, _events(EventType.RECRUIT_STARTED))
    assert out[0].startswith("  📋 规则：")
    assert spy.observed == [(3, True)]


def test_reconnect_without_recruit_started_still_advises():
    # 重连时 STEP=MAIN_ACTION 以初始值出现，不会有招募开始事件
    out = []
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append))
    state = make_state(turn=4, gold=6, shop=[shop_minion(20, "BGS_061", 3, 1)])
    runner.on_update(state, _events(EventType.GOLD_CHANGED, EventType.SHOP_CHANGED, turn=4))
    assert out and out[0].startswith("  📋 规则：")


def test_hero_power_change_updates_rules_advice():
    # 招募开始时选项还没到（技能状态未知）；选项到了、技能能用，规则建议跟着更新
    from factory import power

    out = []
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append))
    shop = [shop_minion(20, "BGS_061", 3, 1)]
    unknown = make_state(turn=2, gold=0, shop=shop, hero_power_state=power(option="unknown"))
    runner.on_update(unknown, _events(EventType.RECRUIT_STARTED))
    usable = make_state(turn=2, gold=0, shop=shop, seq=2, hero_power_state=power())
    runner.on_update(usable, _events(EventType.HERO_POWER_CHANGED))
    assert out == [
        "  📋 规则：无需操作（本回合不需要操作）",
        "  📋 规则：① 使用英雄技能 BG21_HERO_000p（免费技能，每回合都用掉）",
    ]


def hero_log_lines():
    from helpers import HERO_LOG

    from lscs.replay import read_log_lines

    return list(read_log_lines(HERO_LOG))


def test_hero_rules_shown_for_each_offer_only():
    out = []
    spy = SpyScheduler()
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append), spy)
    run_with_runner(hero_log_lines(), runner)
    reason = "这几个英雄里它护甲最少：官方给越强的英雄发越少的护甲"
    # 开局一次、重随一次；其他玩家的英雄出现、选完后的等待都不再提示
    assert [line for line in out if "选 " in line or "护甲" in line] == [
        f"  📋 规则：选 BG21_HERO_000（{reason}）"
    ] * 2
    assert runner.last_hero.pick == 11
    assert (15, "BG22_HERO_003") in runner.last_hero.signature  # 重随后的那一组
    # 选英雄阶段的每次更新都交给调度器（没有 ctx）
    hero_updates = [has_ctx for phase, has_ctx in spy.phases if phase == "HERO_SELECT"]
    assert hero_updates and not any(hero_updates)


def test_hero_rules_error_does_not_stop(monkeypatch):
    import lscs.advisor.runner

    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(lscs.advisor.runner, "rule_hero_pick", boom)
    out = []
    spy = SpyScheduler()
    runner = AdviceRunner(RULES, lambda c: c, AdviceDisplay(lambda c: c, out.append), spy)
    run_with_runner(hero_log_lines(), runner)
    assert out[0] == "  📋 选英雄规则建议出错"
    assert out.count("  📋 选英雄规则建议出错") == 1  # 重随时不再重复提示
    assert "  📋 本回合规则建议出错" not in out
    assert any(phase == "HERO_SELECT" for phase, _ in spy.phases)  # AI 照常调用


class RecordingDisplay:
    def __init__(self):
        self.calls = []

    def __getattr__(self, method):
        return lambda *args: self.calls.append((method, args))


def test_choice_offered_before_main_action_lists_the_options():
    from factory import choice_event, choice_state, resolved_event

    display = RecordingDisplay()
    runner = AdviceRunner(RULES, lambda c: c, display)
    # 回合开始时的技能升级选项：还没到 MAIN_ACTION，招募建议不出，选项照样交给显示端
    state = choice_state(main_action=False, turn=6)
    runner.on_update(state, [choice_event(state)])
    assert display.calls == [("show_choice", (state, None))]
    done = make_state(turn=6, seq=2, main_action=False)
    runner.on_update(done, [resolved_event(12, turn=6, picked=6933)])
    assert len(display.calls) == 1  # 选完不再提示


def test_opponents_overview_once_per_turn_before_the_rules():
    from lscs.state.models import OpponentView

    display = RecordingDisplay()
    runner = AdviceRunner(RULES, lambda c: c, display)
    rival = OpponentView(player_id=3, hero_card_id="H", health=30, armor=0, place=2, tavern_tier=2)
    shop = [shop_minion(20, "BGS_061", 3, 1)]

    def state(turn, seq, gold=4, **kwargs):
        made = make_state(turn=turn, gold=gold, shop=shop, seq=seq, **kwargs)
        return made.model_copy(update={"opponents": {3: rival}, "next_opponent_id": 3})

    runner.on_update(state(2, 1), _events(EventType.RECRUIT_STARTED))
    runner.on_update(state(2, 2, gold=2), _events(EventType.GOLD_CHANGED))  # 同一回合：不再列
    runner.on_update(state(2, 3, phase=Phase.COMBAT), _events(EventType.COMBAT_STARTED))
    # 重连：没有招募开始事件，第一次看到这个回合也列一次
    runner.on_update(state(3, 4), _events(EventType.SHOP_CHANGED, turn=3))
    methods = [name for name, _ in display.calls]
    assert methods == [
        "show_opponents",
        "show_rules",
        "show_position",  # 每次算完规则都告诉显示端站位（这里没有卡牌数据：None）
        "show_rules",
        "show_position",
        "show_opponents",
        "show_rules",
        "show_position",
    ]
    _, (shown, overview) = display.calls[0]
    assert shown.seq == 1 and overview.turn == 2
    assert [s.player_id for s in overview.opponents] == [3]


def test_no_overview_without_opponents():
    display = RecordingDisplay()
    runner = AdviceRunner(RULES, lambda c: c, display)
    runner.on_update(make_state(turn=2, gold=4), _events(EventType.RECRUIT_STARTED))
    assert [name for name, _ in display.calls] == ["show_rules", "show_position"]


# ---- 站位（站位建议设计文档第 5.1 节） ----

from helpers import FIXTURES  # noqa: E402

from lscs.advisor.display import DisplayGroup  # noqa: E402
from lscs.cards.db import CardDB  # noqa: E402

CARDS = CardDB.from_json_file(FIXTURES / "cards_mini.json")
SPAWN = "BG36_116"  # 孢林卵怪：战斗型亡语


def positions(display):
    return [args[1] for method, args in display.calls if method == "show_position"]


def pos_state(*order, gold=0, seq=1, turn=3, shop=(), phase=Phase.RECRUIT):
    """场上：101 蚀脑怪、102 虚悯者、103 孢林卵怪，按 order 给出的顺序摆。"""
    cards = {101: "BG36_099", 102: "BG36_098", 103: SPAWN}
    from factory import minion

    board = [minion(e, cards[e], 2, 2) for e in order]
    return make_state(turn=turn, gold=gold, seq=seq, board=board, shop=shop, phase=phase)


def pos_runner(display, **kwargs):
    return AdviceRunner(RULES, CARDS.name, display, cards=CARDS, **kwargs)


def test_position_only_after_buying_and_done_after_dragging():
    display = RecordingDisplay()
    runner = pos_runner(display)
    shop = [shop_minion(20, "BGS_061", 3, 1)]
    runner.on_update(
        pos_state(101, 102, 103, gold=2, shop=shop), _events(EventType.RECRUIT_STARTED)
    )
    runner.on_update(pos_state(101, 102, 103, seq=2), _events(EventType.GOLD_CHANGED))
    runner.on_update(pos_state(103, 101, 102, seq=3), _events(EventType.BOARD_CHANGED))
    first, bought, dragged = positions(display)
    assert first is None  # 规则方案里还有买：不打断买牌
    assert [(m.entity_id, m.index) for m in bought.moves] == [(103, 0)]
    assert (bought.state_seq, bought.turn, bought.intro) == (2, 3, True)
    assert dragged.moves == () and not dragged.intro
    # 显示端的顺序：规则在前，站位在后
    methods = [m for m, _ in display.calls]
    assert methods.index("show_rules") < methods.index("show_position")


def test_refresh_or_hero_power_left_in_the_plan_still_counts_as_bought():
    from factory import power

    display = RecordingDisplay()
    runner = pos_runner(display)
    board = pos_state(101, 102, 103).me.board
    # 1 金：买不起（米尔豪斯 2 金一张），还能刷新、能用免费技能
    state = make_state(turn=3, gold=1, board=board, hero_power_state=power())
    runner.on_update(state, _events(EventType.RECRUIT_STARTED))
    assert [a.type for a in runner.last_rules.plan.actions] == ["hero_power", "refresh"]
    (advice,) = positions(display)
    assert advice is not None and advice.moves


def test_no_position_without_card_data_or_when_switched_off():
    for kwargs in ({"cards": None}, {"cards": CARDS, "positioning": False}):
        display = RecordingDisplay()
        runner = AdviceRunner(RULES, CARDS.name, display, **kwargs)
        runner.on_update(pos_state(101, 102, 103), _events(EventType.RECRUIT_STARTED))
        runner.on_update(pos_state(101, 103, 102, seq=2), _events(EventType.BOARD_CHANGED))
        assert positions(display) == [None, None]


def test_position_intro_once_per_game():
    display = RecordingDisplay()
    runner = pos_runner(display)
    runner.on_update(pos_state(101, 102, 103), _events(EventType.RECRUIT_STARTED))
    runner.on_update(pos_state(101, 103, 102, seq=2), _events(EventType.BOARD_CHANGED))
    runner.on_update(pos_state(101, 102, 103, seq=3, turn=4), _events(EventType.RECRUIT_STARTED))
    new_game = make_state(turn=0, seq=4, phase=Phase.HERO_SELECT)
    runner.on_update(new_game, [Event(type=EventType.GAME_STARTED, turn=0)])
    runner.on_update(pos_state(101, 102, 103, seq=5), _events(EventType.RECRUIT_STARTED))
    assert [a.intro for a in positions(display)] == [True, False, False, True]


def test_rules_error_falls_back_to_gold(monkeypatch):
    import lscs.advisor.runner

    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(lscs.advisor.runner, "rule_plans", boom)
    display = RecordingDisplay()
    runner = pos_runner(display)
    runner.on_update(pos_state(101, 102, 103, gold=2), _events(EventType.RECRUIT_STARTED))
    runner.on_update(pos_state(101, 102, 103, gold=1, seq=2), _events(EventType.GOLD_CHANGED))
    first, second = positions(display)
    assert first is None  # 米尔豪斯买一张 2 金：还买得起
    assert second.moves


def test_invalid_or_failing_position_is_only_logged(monkeypatch, caplog):
    import lscs.advisor.runner

    monkeypatch.setattr(lscs.advisor.runner, "validate_position", lambda *_args: "坏了")
    display = RecordingDisplay()
    runner = pos_runner(display)
    with caplog.at_level("WARNING"):
        runner.on_update(pos_state(101, 102, 103), _events(EventType.RECRUIT_STARTED))
    assert positions(display) == [None]
    assert "站位建议不合法（坏了）" in caplog.text

    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(lscs.advisor.runner, "plan_positions", boom)
    caplog.clear()
    with caplog.at_level("WARNING"):
        for seq in (2, 3):
            runner.on_update(pos_state(101, 102, 103, seq=seq), _events(EventType.GOLD_CHANGED))
    assert positions(display) == [None, None, None]
    assert caplog.text.count("计算站位建议时出错") == 1  # 完整堆栈只记一次
    assert "rules_error" not in [m for m, _ in display.calls]  # 规则建议照常


def test_position_lines_in_the_terminal():
    out = []
    runner = pos_runner(DisplayGroup(AdviceDisplay(CARDS.name, out.append)))
    runner.on_update(pos_state(101, 102, 103), _events(EventType.RECRUIT_STARTED))
    runner.on_update(pos_state(103, 101, 102, seq=2), _events(EventType.BOARD_CHANGED))
    assert [line for line in out if "🧭" in line] == [
        "  🧭 站位规则：战斗时随从从最左边开始依次出手；亡语召唤的随从出现在原位；"
        "嘲讽会被优先攻击。本局只提示这一次",
        "  🧭 站位：把 左数第 3 个 孢林卵怪 拖到最左边"
        "（亡语随从先出手、先倒下，亡语最早生效，后面的随从都能用上）",
        "  🧭 站位已调好",
    ]


# ---- 本版本统计（本版本统计设计文档第 6.3、7.1 节） ----


def _trinket_state(kind="trinket"):
    from factory import choice_option, choice_state, make_choice

    options = [
        choice_option(3027, "BG35_MagicItem_890", 0, card_type="BATTLEGROUND_TRINKET", cost=4),
        choice_option(3026, "BG36_MagicItem_402", 1, card_type="BATTLEGROUND_TRINKET", cost=4),
    ]
    choice = make_choice(options, choice_id=6, kind=kind, source="BG30_Trinket_1st")
    return choice_state(choice, turn=6, main_action=False)


@NEEDS_STATS_FIXTURES
def test_hero_stats_shown_once_before_the_rules_pick():
    from factory import hero_select_state, offered_event
    from helpers import fixture_stats

    display = RecordingDisplay()
    stats = fixture_stats()
    runner = AdviceRunner(RULES, lambda c: c, display, stats=lambda: stats)
    state = hero_select_state()
    runner.on_update(state, [offered_event(state)])
    runner.on_update(state.model_copy(update={"seq": 2}), [])  # 同一组备选：不再列
    assert [method for method, _ in display.calls] == ["show_stats", "show_hero"]
    note = display.calls[0][1][1]
    assert (note.kind, note.best, note.signature) == ("hero", 89, state.hero_select.signature())
    hero = display.calls[1][1][1]
    assert (hero.pick, hero.basis) == (89, "stats") and runner.last_hero is hero


@NEEDS_STATS_FIXTURES
def test_trinket_stats_shown_before_the_choice_and_only_for_trinkets():
    from factory import choice_event
    from helpers import fixture_stats

    display = RecordingDisplay()
    stats = fixture_stats()
    runner = AdviceRunner(RULES, lambda c: c, display, stats=lambda: stats)
    state = _trinket_state()
    runner.on_update(state, [choice_event(state)])
    assert [method for method, _ in display.calls] == ["show_stats", "show_choice"]
    note = display.calls[0][1][1]
    assert (note.kind, note.choice_id, note.best) == ("trinket", 6, 3026)
    display.calls.clear()
    stance = _trinket_state("hero_power")
    runner.on_update(stance, [choice_event(stance)])
    assert [method for method, _ in display.calls] == ["show_choice"]  # 其他种类不出条目


def test_no_stats_means_no_stats_rows_and_armor_rules():
    from factory import choice_event, hero_select_state, offered_event

    for stats in (None, lambda: None):  # 统计关闭；打开了但还没准备好
        display = RecordingDisplay()
        runner = AdviceRunner(RULES, lambda c: c, display, stats=stats)
        state = hero_select_state()
        runner.on_update(state, [offered_event(state)])
        trinkets = _trinket_state()
        runner.on_update(trinkets, [choice_event(trinkets)])
        assert [method for method, _ in display.calls] == ["show_hero", "show_choice"]
        assert display.calls[0][1][1].basis == "armor"


def test_stats_errors_do_not_stop_the_rules(caplog):
    from factory import hero_select_state, offered_event

    def boom():
        raise RuntimeError("坏了")

    display = RecordingDisplay()
    runner = AdviceRunner(RULES, lambda c: c, display, stats=boom)
    state = hero_select_state()
    runner.on_update(state, [offered_event(state)])
    assert [method for method, _ in display.calls] == ["show_hero"]
    assert "坏了" in caplog.text
