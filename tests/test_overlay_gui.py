"""图形界面冒烟测试：真的创建悬浮面板窗口，渲染几种状态后关闭。

默认不运行；在有显示器的机器上用 `uv run pytest -m gui` 运行。
"""

import pytest
from factory import make_state, shop_minion

from lscs.advisor.models import Action, Advice, Plan
from lscs.config import OverlayConfig
from lscs.overlay.model import OverlayModel
from lscs.state.models import Event, EventType, Phase

pytestmark = pytest.mark.gui

NAMES = {"B": "海盗", "C": "蚀脑怪", "H2": "洛卡拉", "H3": "厄祖玛特", "H5": "詹迪斯"}
STATE = make_state(turn=3, gold=5, tier=2, upgrade_cost=6, shop=[shop_minion(20, "B", 3, 2)])


def rivals():
    from factory import minion

    from lscs.state.models import OpponentView

    def opp(player_id, health, place, board=(), seen=None):
        return OpponentView(
            player_id=player_id,
            hero_card_id=f"H{player_id}",
            health=health,
            armor=0,
            place=place,
            tavern_tier=4,
            last_board=tuple(board),
            last_seen_turn=seen,
        )

    big = [minion(30, "C", 20, 20, races=["ABERRATION"])]
    return {2: opp(2, 30, 1), 3: opp(3, 25, 2, big, 2), 5: opp(5, 0, 8, big, 1)}


# 战斗中：面板列出所有对手（第 3 回合打的是已淘汰的詹迪斯的残影）
COMBAT = make_state(turn=3, phase=Phase.COMBAT, seq=3).model_copy(
    update={"opponents": rivals(), "next_opponent_id": 5}
)


def test_overlay_window_renders_states_and_closes():
    tk = pytest.importorskip("tkinter")
    from lscs.overlay.window import OverlayWindow

    try:
        window = OverlayWindow(OverlayConfig(position="right", opacity=0.9))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    model = OverlayModel(lambda c: NAMES.get(c, c))
    rules = Plan(
        source="rules",
        actions=(Action(type="buy", entity_id=20), Action(type="refresh")),
        direction="海盗",
    )
    ai = Plan(source="llm", actions=(Action(type="upgrade"),), summary="血量健康，先升本" * 4)
    deep = Plan(
        source="llm",
        actions=(Action(type="buy", entity_id=20),),
        analysis="畸变核心在 5 本，血量健康，提前升本。" * 2,
        direction="畸变",
        look_for=("神明", "飘荡祭品"),
    )
    steps = [
        lambda: model.on_update(STATE, [Event(type=EventType.RECRUIT_STARTED, turn=3)]),
        lambda: model.show_rules(STATE, Advice(state_seq=1, turn=3, plan=rules)),
        lambda: model.ai_pending(),
        lambda: model.ai_pending("deep"),
        lambda: model.show_ai(
            STATE, Advice(state_seq=1, turn=3, plan=ai, stale_note="基于 6 秒前的局面"), False
        ),
        lambda: model.show_ai(STATE, Advice(state_seq=1, turn=3, plan=deep, lane="deep"), False),
        lambda: model.on_update(STATE.model_copy(update={"uncertain": True, "seq": 2}), []),
        lambda: model.on_update(
            STATE.model_copy(update={"seq": 3, "opponents": rivals(), "next_opponent_id": 3}), []
        ),
        lambda: model.on_update(make_state(turn=3, phase=Phase.COMBAT, seq=4), []),
        lambda: model.on_update(COMBAT.model_copy(update={"seq": 5}), []),
        lambda: model.show_message("监听已停止，请查看终端"),
    ]
    snapshots = []
    polls = 0

    def keep_running():
        nonlocal polls
        polls += 1
        if polls % 3:  # 每一步之后留两次刷新，让窗口画出来
            return True
        snapshots.append((window.texts(), window.size))
        if not steps:
            return False
        steps.pop(0)()
        return True

    window.run(model, keep_running)
    texts = [t for shown, _ in snapshots for t in shown]
    flat = [t.replace("\n", "") for t in texts]
    # 页眉（灰色小字）+ 右侧来源；"下一步"；带标签的短句
    assert "第 3 回合 · 铸币 5 · 2 本 · 升本 6" in texts
    assert "规则" in texts and "· 分析中…" in texts and "AI 深度" in texts
    assert "▶ 买 左数第 1 个 海盗" in texts and "商店 ●" in texts
    assert "然后" in texts and "② 刷新" in texts and "方向" in texts and "海盗" in texts
    assert "目标" in texts and any(t.startswith("畸变核心在 5 本") for t in flat)
    # 不再有按来源分的区（规则 / AI（深度） / 对手）、要找、同意规则建议
    assert "AI（深度）" not in texts and "对手" not in texts
    assert not any("要找" in t or "① 买" in t for t in texts)
    assert "⚠ 状态可能不准" in texts
    assert "第 3 回合 · 战斗中" in texts
    # 战斗中：标题带正在打的对手，下面一行"最强"；招募时不写对手
    assert "第 3 回合 · 战斗中 · 对手 詹迪斯" in texts
    assert "最强" in texts and "厄祖玛特 · 畸变×1 身材 40" in texts
    assert not any("▶★" in t or "残影" in t for t in texts)
    assert snapshots[-1][0] == ["监听已停止，请查看终端"]
    full = next(size for shown, size in snapshots if "⚠ 状态可能不准" in shown)
    compact = snapshots[-1][1]
    assert compact[1] < full[1]  # 只有一行时面板变矮
    assert all(w > 0 and h > 0 for _, (w, h) in snapshots)
    assert all(h <= window._layout.max_height for _, (_w, h) in snapshots)


def test_overlay_window_renders_hero_select():
    tk = pytest.importorskip("tkinter")
    from factory import hero_select_state, offered_event

    from lscs.advisor.models import HeroAdvice, HeroComment
    from lscs.overlay.window import OverlayWindow

    try:
        window = OverlayWindow(OverlayConfig(position="right", opacity=0.9))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    names = {
        "BG21_HERO_000": "凯瑞尔·罗姆",
        "BG28_HERO_800": "泰瑟兰·血望者",
        "BG33_HERO_001": "洛，在世传奇",
        "TB_BaconShop_HERO_91": "了不起的杰弗里斯",
    }
    model = OverlayModel(lambda c: names.get(c, c))
    state = hero_select_state()
    signature = state.hero_select.signature()
    rules = HeroAdvice(source="rules", signature=signature, pick=89, reason="护甲最少")
    comments = ((90, "要靠对子才强，前期慢"), (91, "技能复杂，新手难发挥"), (92, "发现随机"))
    ai = HeroAdvice(
        source="llm",
        signature=signature,
        pick=89,
        reason="0 费技能每回合白送属性，新手也能稳定用",
        comments=tuple(HeroComment(entity_id=e, text=t) for e, t in comments),
    )
    steps = [
        lambda: model.on_update(state, [offered_event(state)]),
        lambda: model.show_hero(state, rules),
        lambda: model.ai_pending("hero"),
        lambda: model.show_hero(state, ai, True),
        lambda: model.on_update(hero_select_state(seq=2, chosen="BG21_HERO_000"), []),
    ]
    snapshots = []
    polls = 0

    def keep_running():
        nonlocal polls
        polls += 1
        if polls % 3:
            return True
        snapshots.append((window.texts(), window.size))
        if not steps:
            return False
        steps.pop(0)()
        return True

    window.run(model, keep_running)
    texts = [t for shown, _ in snapshots for t in shown]
    flat = [t.replace("\n", "") for t in texts]
    assert "选英雄 · 护甲越少越强" in texts
    assert "▶ 选 左数第 1 个 凯瑞尔·罗姆" in texts and "英雄 ● ○ ○ ○" in texts
    assert "护甲" in texts and any(t.startswith("★凯瑞尔·罗姆 10 · ") for t in flat)
    assert "规则" in texts and "· 分析中…" in texts and "AI" in texts
    assert "理由" in texts and "0 费技能每回合白送属性，新手也能稳定用" in flat
    assert not any("要靠对子才强" in t or "✓" in t for t in texts)  # 点评只在终端和详细模式
    assert snapshots[-1][0] == ["已选 凯瑞尔·罗姆 · 等待招募阶段"]
    assert all(h <= window._layout.max_height for _, (_w, h) in snapshots)


def test_overlay_window_condenses_when_too_tall():
    tk = pytest.importorskip("tkinter")
    from lscs.overlay.window import OverlayWindow

    try:
        window = OverlayWindow(OverlayConfig(font_size=40))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    model = OverlayModel(lambda c: NAMES.get(c, c))
    state = STATE.model_copy(update={"uncertain": True})
    model.on_update(state, [Event(type=EventType.RECRUIT_STARTED, turn=3)])
    steps = tuple(Action(type="refresh") for _ in range(6))
    plan = Plan(source="llm", actions=steps, summary="很长的思路" * 8, direction="海盗")
    model.show_rules(
        state, Advice(state_seq=1, turn=3, plan=plan.model_copy(update={"source": "rules"}))
    )
    model.show_ai(
        state, Advice(state_seq=1, turn=3, plan=plan, stale_note="基于 6 秒前的局面"), False
    )
    deep = plan.model_copy(update={"analysis": "很长的分析" * 8, "look_for": ("神明",) * 6})
    model.show_ai(state, Advice(state_seq=1, turn=3, plan=deep, lane="deep"), False)
    model.ai_pending("deep")  # 只是为了多一行
    model.show_ai(state, Advice(state_seq=1, turn=3, plan=deep, lane="deep"), False)
    seen = []

    def keep_running():
        seen.append((window.texts(), window.size))
        return len(seen) < 4

    window.run(model, keep_running)
    texts, (_w, height) = seen[-1]
    assert height <= window._layout.max_height
    assert texts[0].startswith("第 3 回合")  # 页眉、"下一步"和警告总是保留（字大时会换行）
    assert any(t.startswith("▶") for t in texts)
    if "然后" in texts:  # 精简时先去掉次要的"然后"，"目标"留着
        assert "目标" in texts
    assert "⚠ 状态可能不准" in texts or height == window._layout.max_height


def test_overlay_window_renders_detail_mode():
    tk = pytest.importorskip("tkinter")
    from lscs.overlay.window import OverlayWindow

    try:
        window = OverlayWindow(OverlayConfig(position="right", opacity=0.9, detail=True))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    model = OverlayModel(lambda c: NAMES.get(c, c), detail=True)
    rules = Plan(
        source="rules",
        actions=(Action(type="buy", entity_id=20), Action(type="refresh")),
        direction="海盗",
    )
    deep = Plan(
        source="llm",
        actions=(Action(type="upgrade"), Action(type="refresh")),
        summary="提前升本找畸变核心",
        look_for=("神明",),
    )
    rich = STATE.model_copy(
        update={
            "me": STATE.me.model_copy(update={"gold": 9}),
            "opponents": rivals(),
            "next_opponent_id": 3,
        }
    )
    steps = [
        lambda: model.on_update(rich, [Event(type=EventType.RECRUIT_STARTED, turn=3)]),
        lambda: model.show_rules(rich, Advice(state_seq=1, turn=3, plan=rules)),
        lambda: model.show_ai(rich, Advice(state_seq=1, turn=3, plan=deep, lane="deep"), False),
        lambda: model.on_update(COMBAT.model_copy(update={"seq": 5}), []),
    ]
    snapshots = []
    polls = 0

    def keep_running():
        nonlocal polls
        polls += 1
        if polls % 3:
            return True
        snapshots.append((window.texts(), window.size))
        if not steps:
            return False
        steps.pop(0)()
        return True

    window.run(model, keep_running)
    recruit = next(shown for shown, _ in snapshots if "AI 深度" in shown)
    # 页眉和来源、"现在"块、分区的块标签；分隔线和底色块不算文字
    assert recruit[:2] == ["第 3 回合 · 铸币 9 · 2 本 · 升本 6", "AI 深度"]
    assert recruit[2:3] == ["▶ 升本 → 3 本（6 金）"]
    assert "本回合" in recruit and "留意" in recruit
    assert "① 升本 → 3 本" in recruit and "② 刷新" in recruit
    assert "目标：提前升本找畸变核心" in recruit
    assert any(t.startswith("规则另议：买 海盗") for t in recruit)
    assert any(t.startswith("要找 神明") for t in recruit)
    assert any(t.startswith("下个对手 厄祖玛特") for t in recruit)
    assert "" not in recruit
    combat = snapshots[-1][0]
    assert combat[:2] == ["第 3 回合 · 战斗中 · 对手 詹迪斯", "对手"]
    assert any(t.startswith("对手 第8名 詹迪斯 已淘汰（残影）") for t in combat)
    assert all(0 < h <= window._layout.max_height for _, (_w, h) in snapshots)


def test_detail_mode_condenses_block_by_block():
    tk = pytest.importorskip("tkinter")
    from lscs.overlay.window import OverlayWindow

    try:
        window = OverlayWindow(OverlayConfig(font_size=40, detail=True))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    model = OverlayModel(lambda c: NAMES.get(c, c), detail=True)
    state = STATE.model_copy(
        update={"uncertain": True, "opponents": rivals(), "next_opponent_id": 3}
    )
    model.on_update(state, [Event(type=EventType.RECRUIT_STARTED, turn=3)])
    steps = tuple(Action(type="refresh") for _ in range(5))
    plan = Plan(source="llm", actions=steps, summary="很长的思路" * 8, look_for=("神明",) * 6)
    model.show_rules(
        state,
        Advice(state_seq=1, turn=3, plan=Plan(source="rules", actions=(Action(type="upgrade"),))),
    )
    model.show_ai(state, Advice(state_seq=1, turn=3, plan=plan), False)
    seen = []
    full = {}

    def keep_running():
        if len(seen) == 3:  # 不精简时要多高（量完再按正常的流程画回去）
            full["height"] = window._draw_level(model.snapshot(), window._layout, 0)
            window._draw()
        seen.append((window.texts(), window.size))
        return len(seen) < 4

    window.run(model, keep_running)
    texts, (_w, height) = seen[-1]
    assert height <= window._layout.max_height
    assert texts[0].startswith("第 3 回合")
    assert "本回合" in texts and "① 刷新" in texts  # 本回合的步骤总是保留
    if full["height"] > window._layout.max_height:  # 屏幕不够高（40 像素字）：先去掉"留意"块
        assert "留意" not in texts


def test_header_keeps_the_source_in_its_column_on_a_narrow_panel():
    tk = pytest.importorskip("tkinter")
    from lscs.overlay.model import NextStep, OverlayView
    from lscs.overlay.window import OverlayWindow

    try:
        window = OverlayWindow(OverlayConfig(font_size=40, drag_handle=False))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    view = OverlayView(
        title="第 13 回合 · 铸币 10 · 6 本 · 已满本",
        compact=False,
        next_step=NextStep("▶ 刷新商店"),
        source="AI 深度",
        source_note="· 分析中…",
    )

    class Fixed:
        def snapshot(self):
            return view

    seen = {}

    def keep_running():
        if window._layout is not None and "header" not in seen:
            window._root.update_idletasks()
            header = window._frame.winfo_children()[0]
            labels = header.winfo_children()
            seen["header"] = [(w.cget("text"), w.winfo_x(), w.winfo_width()) for w in labels]
            seen["width"] = header.winfo_width()
        return "header" not in seen

    window.run(Fixed(), keep_running)
    (title, title_x, title_w), (source, src_x, src_w), (note, note_x, note_w) = seen["header"]
    assert (source, note) == ("AI 深度", "· 分析中…")
    assert "\n" in title  # 字大、面板窄：标题折行
    assert title_x + title_w <= src_x < note_x  # 来源和灰字留在右边自己的一列，不压标题
    assert note_x + note_w <= seen["width"]  # 也不出面板


def test_overlay_window_renders_the_review_after_the_game():
    tk = pytest.importorskip("tkinter")
    from dataclasses import replace
    from datetime import datetime

    from test_review_flags import RULES, curve_turns, record

    from lscs.overlay.window import OverlayWindow
    from lscs.review.flags import Flag
    from lscs.review.report import review_game

    try:
        window = OverlayWindow(OverlayConfig(position="right", opacity=0.9))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    model = OverlayModel(lambda c: NAMES.get(c, c))
    over = make_state(phase=Phase.GAME_OVER, seq=9).model_copy(update={"final_place": 3})
    moments = (
        Flag(
            kind="upgrade_without_board",
            turns=(7, 8),
            severity=3,
            facts="连续升本",
            fix="升本也买",
            streak_turns=(8, 9),
            streak_lost=30,
        ),
        Flag(kind="good_turn", turns=(11,), severity=0, facts="凑出金色蚀脑怪，打出 10 血", fix=""),
    )
    review = replace(
        review_game(record(curve_turns()), RULES, lambda c: c, datetime(2026, 10, 11, 23, 20)),
        moments=moments,
        focus="升本的回合也要买一张随从",
        shown_path="reviews/2026-10-11_2320_凯瑞尔·罗姆_第3名.md",
    )
    steps = [
        lambda: model.on_update(over, [Event(type=EventType.GAME_ENDED, turn=13)]),
        lambda: model.show_review(over, review),
    ]
    snapshots = []
    polls = 0

    def keep_running():
        nonlocal polls
        polls += 1
        if polls % 3:
            return True
        snapshots.append((window.texts(), window.size))
        if not steps:
            return False
        steps.pop(0)()
        return True

    window.run(model, keep_running)
    texts = snapshots[-1][0]
    assert texts[:2] == ["对局结束 · 第 3 名", "① 第 7、8 回合 升本不补场面 → 连掉 30 血"]
    assert "② 第 11 回合 做得好：凑出金色蚀脑怪" in texts
    assert "下局重点：升本的回合也要买一张随从" in [t.replace("\n", "") for t in texts]
    assert not any(t.startswith("已保存") for t in texts)  # 文件路径只在终端和详细模式
    compact = next(size for shown, size in snapshots if shown == ["对局结束 · 第 3 名"])
    assert snapshots[-1][1][1] > compact[1]  # 有复盘时面板变高
    assert all(h <= window._layout.max_height for _, (_w, h) in snapshots)


def test_windows_path_with_fake_win32(monkeypatch):
    """在 Mac 上用假的 Win32 API 走一遍 Windows 的代码：贴靠、隐藏、连续失败后退回。"""
    tk = pytest.importorskip("tkinter")
    from fake_win32 import FakeWin32Api, FakeWindow

    from lscs.overlay.window import OverlayWindow

    hs = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"
    api = FakeWin32Api(
        windows=[FakeWindow(1, pid=10, size=(1600, 900), origin=(100, 50))], paths={10: hs}
    ).install(monkeypatch)
    api.ancestor = None  # 面板、标签各有各的句柄
    api.foreground = 1  # 炉石在前台
    out = []
    try:
        window = OverlayWindow(OverlayConfig(), out=out.append)
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    assert window.status.startswith("悬浮面板已打开：炉石在前台时显示在游戏窗口右下角")
    model = OverlayModel(lambda c: c)
    model.on_update(STATE, [Event(type=EventType.RECRUIT_STARTED, turn=3)])
    seen = {}

    def broken():
        raise OSError("GetForegroundWindow 坏了")

    def keep_running():
        if "docked" not in seen and window._geometry is not None:
            seen["docked"] = window._geometry
            api.foreground = 999  # 切到别的程序
        elif "docked" in seen and "hidden" not in seen and api.alpha.get(window._hwnd) == 0:
            seen["hidden"] = True
            api.GetForegroundWindow = broken
        return not out

    window.run(model, keep_running)
    width, height, x, y = seen["docked"]
    assert (x, y) == (100 + 1600 - 32 - width, 50 + 882 - height)  # 客户区右下角，各留 2%
    assert width == 359  # 900 高 → 14 像素字（900 / 70 ≈ 13，下限 14）→ 24.5 个字宽 + 内边距
    assert seen.get("hidden")
    assert out == [
        "贴靠炉石窗口连续 5 次失败（GetForegroundWindow 坏了），"
        "面板已移到屏幕右上角；不需要面板可以用 --no-overlay 关闭。"
    ]


def test_failed_setup_breaks_the_reference_cycle(monkeypatch):
    tk = pytest.importorskip("tkinter")
    from lscs.overlay import window as window_module

    roots = []

    class RecordingTk(tk.Tk):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            roots.append(self)

    def boom(_root):
        raise RuntimeError("字体坏了")

    monkeypatch.setattr(window_module.tk, "Tk", RecordingTk)
    monkeypatch.setattr(window_module, "_font_family", boom)
    try:
        with pytest.raises(RuntimeError, match="字体坏了"):
            window_module.OverlayWindow(OverlayConfig())
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    assert "report_callback_exception" not in roots[0].__dict__


def test_overlay_window_renders_next_step_and_choice():
    tk = pytest.importorskip("tkinter")
    from factory import choice_event, choice_state, resolved_event

    from lscs.advisor.models import ChoiceAdvice
    from lscs.overlay.window import OverlayWindow

    try:
        window = OverlayWindow(OverlayConfig(position="right", opacity=0.9))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    names = {"BG36_312": "摧心招募者", "BG33_318": "怒胆喷毒鱼人", "BG36_356": "泰瑞尔", **NAMES}
    model = OverlayModel(lambda c: names.get(c, c))
    choice = choice_state(shop=[shop_minion(20, "B", 3, 2)])
    advice = ChoiceAdvice(choice_id=12, pick=6933, reason="32/32 直接成主力，死亡之拥还能长")
    rules = Plan(source="rules", actions=(Action(type="buy", entity_id=20),))
    after = make_state(turn=9, seq=2, shop=[shop_minion(20, "B", 3, 2)])
    steps = [
        lambda: model.on_update(choice, [choice_event(choice)]),
        lambda: model.ai_pending("choice"),
        lambda: model.show_choice(choice, advice),
        lambda: model.on_update(after, [resolved_event(12, picked=6933)]),
        lambda: model.show_rules(after, Advice(state_seq=2, turn=9, plan=rules)),
    ]
    snapshots = []
    polls = 0

    def keep_running():
        nonlocal polls
        polls += 1
        if polls % 3:
            return True
        snapshots.append((window.texts(), window.size))
        if not steps:
            return False
        steps.pop(0)()
        return True

    window.run(model, keep_running)
    texts = [t for shown, _ in snapshots for t in shown]
    flat = [t.replace("\n", "") for t in texts]
    assert "▶ 等 AI 选…" in texts and "分析中…" in texts  # 没有目标的"下一步"
    assert "黑暗之赐" in texts and "摧心招募者 · 怒胆喷毒鱼人 · 泰瑞尔" in flat
    assert "▶ 选 左数第 3 个 泰瑞尔" in texts and "选项 ○ ○ ●" in texts
    assert "摧心招募者 · 怒胆喷毒鱼人 · ★泰瑞尔" in flat
    assert "理由" in texts and any(t.startswith("32/32 直接成主力") for t in texts)
    last = snapshots[-1][0]
    step = last.index("▶ 买 左数第 1 个 海盗")
    assert last[step + 1] == "商店 ●"
    assert "黑暗之赐" not in last  # 选完：选项那一行消失
    assert all(h <= window._layout.max_height for _, (_w, h) in snapshots)


def test_highlight_preview_draws_only_the_current_step():
    tk = pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay.geometry import Rect
    from lscs.overlay.highlight import HighlightWindow
    from lscs.overlay.model import NextStep, Target

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        root.withdraw()
        window = HighlightWindow(root, HighlightConfig(), preview=True)
        client = Rect(40, 60, 640, 360)
        buy = NextStep("▶ 买 左数第 2 个 海盗", Target(row="shop", index=1, count=3), badge="2")
        window.show(buy, client)
        root.update()
        assert window.geometry == client
        assert window.texts() == ["2"]  # 徽章上写第几步
        assert window.shapes() == 4 * 2 + 2  # 四个角各两笔（深色衬底 + 颜色线）+ 徽章的圆和字
        window.show(NextStep("▶ 刷新商店", Target(button="refresh"), badge="1"), client)
        assert window.texts() == ["1"]
        window.show(NextStep("▶ 等建议…", tone="muted"), client)  # 没有目标：什么都不画
        assert window.shapes() == 0
        window.show(buy, client)
        window.show(buy, None)  # 面板隐藏：清空，位置不动
        assert window.shapes() == 0 and window.geometry == client
        window.destroy()
    finally:
        root.destroy()


def test_panel_drives_the_highlight_with_fake_win32(monkeypatch):
    """在 Mac 上用假的 Win32 走一遍 Windows 的接线：贴靠时把"下一步"和炉石客户区交给高亮，
    炉石不在前台时清空；高亮出错不影响面板。真正的色键窗口用预览窗口代替。"""
    tk = pytest.importorskip("tkinter")
    from fake_win32 import FakeWin32Api, FakeWindow

    from lscs.config import HighlightConfig
    from lscs.overlay.geometry import Rect
    from lscs.overlay.highlight import HighlightWindow
    from lscs.overlay.window import OverlayWindow

    hs = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"
    api = FakeWin32Api(
        windows=[FakeWindow(1, pid=10, size=(1600, 900), origin=(100, 50))], paths={10: hs}
    ).install(monkeypatch)
    api.foreground = 1
    created = []

    def preview(root, cfg):
        created.append(HighlightWindow(root, cfg, preview=True))
        return created[-1]

    try:
        window = OverlayWindow(
            OverlayConfig(),
            out=lambda _l: None,
            highlight=HighlightConfig(),
            make_highlight=preview,
        )
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    assert window.status.endswith("游戏里会用金色框标出下一步要点的牌或按钮。")
    model = OverlayModel(lambda c: NAMES.get(c, c))
    model.on_update(STATE, [Event(type=EventType.RECRUIT_STARTED, turn=3)])
    plan = Plan(source="rules", actions=(Action(type="buy", entity_id=20),))
    model.show_rules(STATE, Advice(state_seq=1, turn=3, plan=plan))
    seen = {}

    def keep_running():
        highlight = created[0]
        if "shown" not in seen and highlight.texts():
            seen["shown"] = (highlight.geometry, highlight.texts())
            api.foreground = 999  # 切到别的程序：面板隐藏，高亮清空
        elif "shown" in seen and "cleared" not in seen and highlight.shapes() == 0:
            seen["cleared"] = True
            highlight.show = lambda *_args: 1 / 0  # 之后高亮一直出错
            api.foreground = 1
        elif "cleared" in seen:
            seen["after"] = seen.get("after", 0) + 1
        return seen.get("after", 0) < 3

    window.run(model, keep_running)
    assert seen["shown"] == (Rect(100, 50, 1600, 900), ["1"])
    assert seen["cleared"]
    assert window._visible  # 高亮出错，面板照常显示


def test_highlight_that_cannot_open_leaves_the_panel_alone(monkeypatch):
    tk = pytest.importorskip("tkinter")
    from fake_win32 import FakeWin32Api, FakeWindow

    from lscs.config import HighlightConfig
    from lscs.overlay.window import OverlayWindow

    hs = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"
    FakeWin32Api(windows=[FakeWindow(1, pid=10)], paths={10: hs}).install(monkeypatch)

    def broken(_root, _cfg):
        raise tk.TclError('bad attribute "-transparentcolor"')

    try:
        window = OverlayWindow(OverlayConfig(), highlight=HighlightConfig(), make_highlight=broken)
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        assert window.status.startswith("悬浮面板已打开：")
        assert window.status.endswith(
            '画面高亮无法开启（bad attribute "-transparentcolor"），只显示面板。'
        )
        assert window._highlight is None
    finally:
        window._release()


def test_highlight_is_off_when_disabled_or_off_windows():
    tk = pytest.importorskip("tkinter")
    import sys

    from lscs.config import HighlightConfig
    from lscs.overlay.window import OverlayWindow

    if sys.platform == "win32":
        pytest.skip("只检查非 Windows")
    try:
        window = OverlayWindow(OverlayConfig(), highlight=HighlightConfig())
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        assert window.status.endswith("画面高亮只在 Windows 上显示。")
        assert window._highlight is None
    finally:
        window._release()
    window = OverlayWindow(OverlayConfig(), highlight=HighlightConfig(enabled=False))
    try:
        assert "画面高亮" not in window.status
    finally:
        window._release()


def test_banner_alone_still_opens_the_highlight_and_expires(monkeypatch):
    """[highlight] enabled = false 但开着战斗结果的横幅：照样开高亮窗口，"下一步"总是 None；
    对局结束那场的横幅过了 20 秒就传 None（定位的定时器自己清掉，不用等新的状态）。"""
    tk = pytest.importorskip("tkinter")
    from factory import make_state
    from fake_win32 import FakeWin32Api, FakeWindow

    from lscs.config import HighlightConfig
    from lscs.overlay.geometry import Rect
    from lscs.overlay.window import BANNER_ON, HIGHLIGHT_ON, OverlayWindow
    from lscs.state.models import CombatResult

    hs = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"
    api = FakeWin32Api(
        windows=[FakeWindow(1, pid=10, size=(1600, 900), origin=(100, 50))], paths={10: hs}
    ).install(monkeypatch)
    api.foreground = 1
    calls = []

    class FakeHighlight:
        hwnd = 0x999

        def __init__(self, _root, cfg):
            calls.append(("open", cfg.enabled))

        def show(self, step, client, banner=None):
            calls.append((step, client, banner))
            return False

        def destroy(self):
            pass

    now = [100.0]
    try:
        window = OverlayWindow(
            OverlayConfig(),
            out=lambda _l: None,
            highlight=HighlightConfig(enabled=False),
            make_highlight=FakeHighlight,
            banner=True,
            clock=lambda: now[0],
        )
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    assert calls == [("open", False)]
    assert window.status.endswith(BANNER_ON) and HIGHLIGHT_ON not in window.status
    model = OverlayModel(lambda c: NAMES.get(c, c), clock=lambda: now[0])
    dead = CombatResult(
        turn=3,
        opponent_id=3,
        opponent_hero="H3",
        outcome="loss",
        ghost=False,
        lost=18,
        dealt=0,
        my_health=-14,
        my_armor=0,
        opponent_health=35,
        opponent_armor=0,
        me_dead=True,
        opponent_dead=False,
    )
    over = make_state(turn=3, phase=Phase.GAME_OVER).model_copy(
        update={"last_combat": dead, "final_place": 3}
    )
    model.on_update(
        over,
        [Event(type=EventType.COMBAT_RESULT, turn=3), Event(type=EventType.GAME_ENDED, turn=3)],
    )
    seen = []
    polls = [0]

    def keep_running():
        polls[0] += 1
        shown = [c for c in calls[1:] if c[1] is not None]
        if not seen and shown and shown[-1][2] is not None:
            seen.append(shown[-1])
            now[0] = 120.5  # 过了 20 秒
        elif seen and shown and shown[-1][2] is None:
            seen.append(shown[-1])
        return len(seen) < 2 and polls[0] < 60

    window.run(model, keep_running)
    assert len(seen) == 2, calls
    step, client, banner = seen[0]
    assert step is None and client == Rect(100, 50, 1600, 900)
    assert (banner.text, banner.expires_at) == ("本场已定：输 · 你掉 18 血 · 你被淘汰", 120.0)
    assert seen[1][0] is None and seen[1][2] is None
    assert all(c[0] is None for c in calls[1:])  # [highlight] 关着：从不画"下一步"


def test_highlight_test_preview_window_draws_once(monkeypatch):
    pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay import calibrate, win32
    from lscs.overlay.layout import CALIBRATION_ROWS

    monkeypatch.setattr(win32, "IS_WINDOWS", False)  # 预览分支（Windows 上也只看几何）
    drawn = []
    real = calibrate.show_tk

    def show(frames, client, seconds, preview, cfg, focus, banner):
        drawn.append((len(frames), preview, banner.text))
        real(frames, client, seconds, preview, cfg, focus, banner)

    out = []
    ok = calibrate.run_highlight_test(
        HighlightConfig(), 0.3, CALIBRATION_ROWS, out=out.append, show=show
    )
    if not ok and out[-1].startswith("× 无法打开高亮窗口"):
        pytest.skip(f"没有图形界面：{out[-1]}")
    assert ok, out
    assert drawn == [(29, True, "本场已定：赢 · 洛卡拉掉 5 血")]


def test_focus_taken_by_the_highlight_is_given_back(monkeypatch):
    tk = pytest.importorskip("tkinter")
    from fake_win32 import FakeWin32Api, FakeWindow

    from lscs.config import HighlightConfig
    from lscs.overlay.window import OverlayWindow

    hs = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"
    api = FakeWin32Api(windows=[FakeWindow(1, pid=10)], paths={10: hs}).install(monkeypatch)
    api.foreground = 0x555  # 运行 watch 的 PowerShell

    class StealingHighlight:
        hwnd = 0x999

        def __init__(self, _root, _cfg):
            api.foreground = self.hwnd  # 第一次显示时抢走了焦点

        def show(self, step, client, banner=None):
            pass

        def destroy(self):
            pass

    try:
        window = OverlayWindow(
            OverlayConfig(), highlight=HighlightConfig(), make_highlight=StealingHighlight
        )
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        assert api.focus_calls == [0x555]
    finally:
        window._release()


def _highlight_window(tk, cfg):
    from lscs.overlay.highlight import HighlightWindow

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    root.withdraw()
    return root, HighlightWindow(root, cfg, preview=True)


def _drawn(window, frame, client):
    """画一个框，返回 (第一个字的位置, 各种图形的个数)。"""
    from collections import Counter

    window.draw([frame], client)
    canvas = window._canvas
    items = canvas.find_all()
    texts = [i for i in items if canvas.type(i) == "text"]
    where = canvas.coords(texts[0]) if texts else None
    return where, dict(Counter(canvas.type(i) for i in items))


def test_highlight_badges_and_calibration_labels_are_drawn_differently():
    tk = pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay.geometry import Rect
    from lscs.overlay.layout import Frame
    from lscs.overlay.style import BADGE_FILL, DARK

    root, window = _highlight_window(tk, HighlightConfig())
    try:
        client = Rect(0, 0, 640, 360)  # 内容区高 360：衬底 4、颜色线 2、徽章半径 9
        canvas = window._canvas
        button = Rect(100, 100, 60, 60)
        # highlight-test 的按钮：完整的圆（衬底 + 颜色线），两个字的标签写在正中间
        calibration = Frame(button, round=True, label="升本", calibration=True)
        assert _drawn(window, calibration, client) == ([130.0, 130.0], {"oval": 2, "text": 1})
        # "下一步"：四个角的括号，徽章在框右上角的正上方（半径 9 + 衬底 4）
        step = Frame(button, round=True, label="1", badge=True)
        assert _drawn(window, step, client) == ([160.0, 87.0], {"line": 8, "oval": 1, "text": 1})
        lines = [i for i in canvas.find_all() if canvas.type(i) == "line"]
        assert [canvas.itemcget(i, "fill") for i in lines] == [DARK] * 4 + ["#f2d98a"] * 4
        assert [canvas.itemcget(i, "width") for i in lines] == ["4.0"] * 4 + ["2.0"] * 4
        badge = next(i for i in canvas.find_all() if canvas.type(i) == "oval")
        assert canvas.itemcget(badge, "fill") == BADGE_FILL
        assert canvas.itemcget(badge, "outline") == "#f2d98a"
        slot = Rect(200, 50, 80, 100)
        calibration_slot = Frame(slot, label="商店1", calibration=True)
        assert _drawn(window, calibration_slot, client) == (
            [240.0, 100.0],
            {"polygon": 2, "text": 1},
        )
        window.destroy()
    finally:
        root.destroy()


def test_highlight_frame_style_draws_a_thin_full_outline():
    tk = pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay.geometry import Rect
    from lscs.overlay.layout import Frame

    root, window = _highlight_window(tk, HighlightConfig(style="frame", color="#80c0ff"))
    try:
        client = Rect(0, 0, 640, 360)
        button = Frame(Rect(100, 100, 60, 60), round=True, label="2", badge=True)
        assert _drawn(window, button, client) == ([160.0, 87.0], {"oval": 3, "text": 1})
        slot = Frame(Rect(200, 50, 80, 100))
        assert _drawn(window, slot, client) == (None, {"polygon": 2})
        canvas = window._canvas
        outlines = [canvas.itemcget(i, "outline") for i in canvas.find_all()]
        assert outlines == ["#281e0a", "#80c0ff"]  # 深色衬底 + 设置的颜色
        window.destroy()
    finally:
        root.destroy()


def test_highlight_draws_the_combat_result_banner_on_top():
    """战斗结果的横幅：棋盘顶部正中一个深色圆角框，边框和字用结果的颜色，画在框之后；
    同样的内容不重画，None 或面板隐藏时清掉，太长时截掉末尾加"…"。"""
    tk = pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay.banner import Banner
    from lscs.overlay.geometry import Rect
    from lscs.overlay.model import NextStep, Target
    from lscs.overlay.style import BADGE_FILL

    root, window = _highlight_window(tk, HighlightConfig())
    try:
        canvas = window._canvas
        client = Rect(40, 60, 1280, 720)  # 内容区高 720：字号 19，框 y 9~39
        won = Banner("本场已定：赢 · 洛卡拉掉 5 血", "win")
        window.draw([], client, won)
        assert window.texts() == [won.text] and window.shapes() == 2
        box, text = canvas.find_all()
        assert canvas.type(box) == "polygon"
        assert canvas.itemcget(box, "fill") == BADGE_FILL
        assert canvas.itemcget(box, "outline") == "#9fd3a0"
        assert canvas.itemcget(text, "fill") == "#9fd3a0"
        x0, y0, x1, y1 = canvas.bbox(box)
        assert 0 <= y0 <= 9 and 39 <= y1 < 0.08 * 720  # 不碰对手头像
        assert (x0 + x1) / 2 == pytest.approx(640, abs=3)  # 画布坐标：客户区正中
        drawn, items = window._drawn, canvas.find_all()
        window.draw([], client, won)  # 同样的内容：不重画
        assert window._drawn is drawn and canvas.find_all() == items
        window.draw([], client, Banner(won.text, "win", expires_at=12.0))  # 到期时间不算内容
        assert canvas.find_all() == items
        step = NextStep("▶ 买", Target(row="shop", index=1, count=3), badge="1")
        window.show(step, client, won)
        assert window.shapes() == 10 + 2  # 四角括号、徽章，再加横幅
        assert canvas.itemcget(canvas.find_all()[-1], "text") == won.text  # 最上层
        lost = Banner("本场已定：输 · 你掉 2 血，剩 30+8", "loss")
        window.show(step, client, lost)
        assert lost.text in window.texts()
        assert canvas.itemcget(canvas.find_all()[-2], "outline") == "#ff9f43"
        window.show(step, client, None)
        assert window.shapes() == 10 and not any("本场已定" in t for t in window.texts())
        window.show(step, None, won)  # 面板隐藏：横幅也清掉
        assert window.shapes() == 0
        long = Banner("本场已定：赢 · " + "非常长的英雄名字" * 20 + "掉 5 血", "tie")
        window.draw([], client, long)
        (shown,) = window.texts()
        assert shown.endswith("…") and shown.startswith("本场已定：赢 · ")
        x0, _y0, x1, _y1 = canvas.bbox(canvas.find_all()[0])
        assert x1 - x0 <= 0.5 * 1280 + 4  # 封顶半个内容区（加上边框）
        assert canvas.itemcget(canvas.find_all()[0], "outline") == "#d4d4dc"
        window.destroy()
    finally:
        root.destroy()


def test_highlight_breathes_once_per_new_target(monkeypatch):
    """新目标出现时颜色线在原色和暗色之间切换几次，然后不动；同一个目标不再呼吸；隐藏、销毁时
    取消定时器。用真的 Tk 定时器（间隔改成 10 毫秒）。"""
    tk = pytest.importorskip("tkinter")
    import time

    from lscs.config import HighlightConfig
    from lscs.overlay import highlight
    from lscs.overlay.geometry import Rect
    from lscs.overlay.model import NextStep, Target
    from lscs.overlay.style import DARK

    monkeypatch.setattr(highlight, "PULSE_MS", 10)
    root, window = _highlight_window(tk, HighlightConfig())
    try:
        canvas = window._canvas
        client = Rect(40, 60, 640, 360)
        buy = NextStep("▶ 买", Target(row="shop", index=1, count=3), badge="1")
        refresh = NextStep("▶ 刷新商店", Target(button="refresh"), badge="2")

        def pending():
            return root.tk.splitlist(root.tk.call("after", "info"))

        def colours():
            lines = [i for i in canvas.find_all() if canvas.type(i) == "line"]
            return {canvas.itemcget(i, "fill") for i in lines}

        def run(seconds):
            seen = []
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                root.update()
                seen.append(colours())
                time.sleep(0.002)
            return seen

        window.show(buy, client)
        assert colours() == {DARK, "#f2d98a"} and pending()  # 先按原色画
        seen = run(0.5)
        assert {DARK, "#978550"} in seen  # 呼吸：颜色线变暗
        assert colours() == {DARK, "#f2d98a"} and not pending()  # 之后一直是原色
        window.show(buy, client)  # 同一个目标：不再呼吸
        assert not pending()
        window.show(refresh, client)  # 换了目标：再呼吸一次
        assert pending()
        window.show(refresh, None)  # 面板隐藏：取消
        assert not pending() and window.shapes() == 0
        window.show(refresh, client)
        assert pending()
        window.destroy()  # 销毁：取消
        assert not pending()
        quiet = highlight.HighlightWindow(root, HighlightConfig(pulse=False), preview=True)
        quiet.show(buy, client)
        assert not pending() and quiet.shapes() == 10
        quiet.destroy()
    finally:
        root.destroy()


def test_highlight_reports_a_rebuilt_window(monkeypatch):
    """Tk 重建了高亮的顶层窗口（新的句柄）：重新加上穿透样式，并告诉面板要重新放到上面。"""
    tk = pytest.importorskip("tkinter")
    from fake_win32 import FakeWin32Api

    from lscs.config import HighlightConfig
    from lscs.overlay.geometry import Rect
    from lscs.overlay.highlight import HighlightWindow
    from lscs.overlay.win32 import PASSTHROUGH_BITS

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        root.withdraw()
        window = HighlightWindow(root, HighlightConfig(), preview=True)  # Mac 的 Tk 没有色键
        api = FakeWin32Api().install(monkeypatch)
        window._preview = False  # 之后按 Windows 的方式加样式（假的 Win32）
        client = Rect(0, 0, 640, 360)
        assert window.show(None, client) is False  # 第一次拿到句柄：不算重建
        assert api.styles[0xABC] & PASSTHROUGH_BITS == PASSTHROUGH_BITS
        assert window.show(None, client) is False
        api.ancestor = 0xDEF  # Tk 重建了顶层窗口
        assert window.show(None, client) is True
        assert window.hwnd == 0xDEF
        assert api.styles[0xDEF] & PASSTHROUGH_BITS == PASSTHROUGH_BITS
        window.destroy()
    finally:
        root.destroy()


def test_panel_stays_when_the_highlight_is_foreground_and_lifts_after_a_rebuild(monkeypatch):
    tk = pytest.importorskip("tkinter")
    from fake_win32 import FakeWin32Api, FakeWindow

    from lscs.config import HighlightConfig
    from lscs.overlay.window import OverlayWindow

    hs = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"
    api = FakeWin32Api(windows=[FakeWindow(1, pid=10)], paths={10: hs}).install(monkeypatch)
    api.foreground = 1

    class FakeHighlight:
        hwnd = 0x999
        rebuilt = True  # 第一次显示时报告重建过

        def __init__(self, _root, _cfg):
            pass

        def show(self, step, client, banner=None):
            rebuilt, FakeHighlight.rebuilt = FakeHighlight.rebuilt, False
            return rebuilt

        def destroy(self):
            pass

    try:
        window = OverlayWindow(
            OverlayConfig(), highlight=HighlightConfig(), make_highlight=FakeHighlight
        )
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    lifts = []
    window._root.lift = lambda *args: lifts.append(args)
    model = OverlayModel(lambda c: NAMES.get(c, c))
    model.on_update(STATE, [Event(type=EventType.RECRUIT_STARTED, turn=3)])
    polls = []

    def keep_running():
        polls.append(window._visible)
        if len(polls) == 2:
            api.foreground = 0x999  # 高亮窗口短暂在前台
        return len(polls) < 6

    window.run(model, keep_running)
    assert lifts == [()]  # 重建过一次：面板重新放到高亮上面
    assert all(polls)  # 高亮在前台时面板也不隐藏


def test_overlay_window_renders_the_position_step():
    tk = pytest.importorskip("tkinter")
    from factory import minion

    from lscs.advisor.models import Move, PositionAdvice
    from lscs.overlay.window import OverlayWindow

    try:
        window = OverlayWindow(OverlayConfig(position="right", opacity=0.9))
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    names = {"T": "泰瑞尔", "SP": "孢林卵怪", **NAMES}
    model = OverlayModel(lambda c: names.get(c, c))
    board = [minion(1, "T", 34, 34), minion(2, "SP", 3, 3), minion(3, "C", 3, 4)]
    state = make_state(turn=9, gold=0, tier=6, upgrade_cost=None, board=board)
    move = Move(
        entity_id=2, index=0, before_id=None, role="deathrattle", reason="", short="亡语最早触发"
    )
    position = PositionAdvice(state_seq=1, turn=9, board=(1, 2, 3), target=(2, 1, 3), moves=(move,))
    steps = [
        lambda: model.on_update(state, [Event(type=EventType.RECRUIT_STARTED, turn=9)]),
        lambda: model.show_rules(state, Advice(state_seq=1, turn=9, plan=Plan(source="rules"))),
        lambda: model.show_position(state, position),
    ]
    snapshots = []
    polls = 0

    def keep_running():
        nonlocal polls
        polls += 1
        if polls % 3:
            return True
        snapshots.append(window.texts())
        if not steps:
            return False
        steps.pop(0)()
        return True

    window.run(model, keep_running)
    last = snapshots[-1]
    step = last.index("▶ 把 左数第 2 个 孢林卵怪 拖到最左边")
    assert last[step + 1 : step + 4] == ["场面 ○ ● ○   拖到最左边", "为何", "亡语最早触发"]


def test_highlight_preview_draws_the_move_badge():
    tk = pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay.geometry import Rect
    from lscs.overlay.highlight import HighlightWindow
    from lscs.overlay.model import BADGE_MOVE, NextStep, Target

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        root.withdraw()
        window = HighlightWindow(root, HighlightConfig(), preview=True)
        client = Rect(40, 60, 640, 360)
        drag = NextStep(
            "▶ 把 左数第 2 个 孢林卵怪 拖到最左边",
            Target(row="board", index=1, count=6),
            slots="场面 ○ ● ○ ○ ○ ○   拖到最左边",
            reason="亡语最早触发",
            badge=BADGE_MOVE,
        )
        window.show(drag, client)
        root.update()
        assert window.texts() == ["拖"]  # 只框要拖的随从，不画目的地
        assert window.shapes() == 4 * 2 + 2
        window.destroy()
    finally:
        root.destroy()


# ---- 拖动标签 ----

HS_EXE = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"
CLIENT = (100, 50, 1600, 900)  # 假炉石的客户区：x, y, 宽, 高
FOCUS_LOST = (
    "注意：炉石失去了焦点（前台是 lscs 的面板）；"
    "如果反复出现，请把 [overlay] drag_handle 设为 false 并告诉开发者"
)


def _fake_hearthstone(monkeypatch):
    """假的 Win32：炉石在前台；面板、高亮、标签各有各的句柄（ancestor=None）。"""
    from fake_win32 import FakeWin32Api, FakeWindow

    x, y, width, height = CLIENT
    api = FakeWin32Api(
        windows=[FakeWindow(1, pid=10, size=(width, height), origin=(x, y))], paths={10: HS_EXE}
    ).install(monkeypatch)
    api.ancestor = None
    api.foreground = 1
    return api


def _open_window(tk, **kwargs):
    from lscs.overlay.window import OverlayWindow

    try:
        return OverlayWindow(OverlayConfig(), **kwargs)
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")


def _once_placed(window, scenario):
    """面板第一次定位好以后，在 Tk 的回调里运行一次 scenario，然后关闭窗口。定时器会吞掉回调里
    的异常，所以先存起来，回来再抛出。"""
    model = OverlayModel(lambda c: NAMES.get(c, c))
    model.on_update(STATE, [Event(type=EventType.RECRUIT_STARTED, turn=3)])
    result = {"polls": 0}

    def keep_running():
        if "done" in result:
            return False
        result["polls"] += 1
        if window._placed is None and result["polls"] < 40:
            return True
        try:
            result["value"] = scenario()
        except Exception as exc:
            result["error"] = exc
        result["done"] = True
        return False

    window.run(model, keep_running)
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _grip_centre(window):
    rect = window._grip.geometry
    return rect.x + rect.width // 2, rect.y + rect.height // 2


def test_grip_sits_above_the_panel_and_hides_with_it(monkeypatch, tmp_path):
    """假 Win32 贴靠：标签不穿透、不抢焦点，面板穿透；默认在右下角，标签在面板正上方、右对齐；
    切到别的程序时两个都透明；标签偶尔在前台时面板不隐藏。"""
    tk = pytest.importorskip("tkinter")
    from lscs.overlay.geometry import Rect
    from lscs.overlay.win32 import (
        WS_EX_LAYERED,
        WS_EX_NOACTIVATE,
        WS_EX_TOOLWINDOW,
        WS_EX_TRANSPARENT,
    )

    api = _fake_hearthstone(monkeypatch)
    out = []
    window = _open_window(tk, out=out.append, position_path=tmp_path / "p.json")
    assert window.status.startswith("悬浮面板已打开：炉石在前台时显示在游戏窗口右下角")
    assert window._grip is not None and window._grip.text() == "≡ 拖动"
    grip_hwnd = window._grip.hwnd
    assert grip_hwnd not in (None, window._hwnd)
    wanted = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    assert api.styles[grip_hwnd] & wanted == wanted
    assert not api.styles[grip_hwnd] & WS_EX_TRANSPARENT  # 标签收得到鼠标
    assert api.styles[window._hwnd] & WS_EX_TRANSPARENT  # 面板照样穿透

    def scenario():
        seen = {"placed": window._placed, "grip": window._grip.geometry}
        seen["panel"] = window._geometry
        api.foreground = grip_hwnd  # 标签成了前台窗口：面板不隐藏
        window._place()
        window._place()  # 只提醒一次
        seen["grip_foreground"] = (window._visible, api.alpha[window._hwnd])
        api.foreground = 999  # 切到别的程序
        window._place()
        seen["hidden"] = (api.alpha[window._hwnd], api.alpha[grip_hwnd], window._placed)
        api.foreground = 1
        window._place()
        seen["shown"] = (api.alpha[window._hwnd], api.alpha[grip_hwnd])
        return seen

    seen = _once_placed(window, scenario)
    x, y, width, height = CLIENT
    area, docked, box = seen["placed"]
    assert (area, docked) == (Rect(*CLIENT), True)
    panel_w, panel_h, panel_x, panel_y = seen["panel"]
    grip = seen["grip"]
    assert panel_y + panel_h == y + 882  # 面板底边 = 客户区底边 − 2%
    assert panel_x + panel_w == x + 1600 - 32  # 右边 − 2%
    assert grip.bottom == panel_y and grip.right == panel_x + panel_w  # 标签在面板正上方、右对齐
    assert (grip.width, grip.height) == (63, 20)  # 900 高 → 14 像素字 → 4.5 × 1.4 个字
    assert box == Rect(panel_x, grip.y, panel_w, panel_h + grip.height)
    assert seen["grip_foreground"] == (True, 217)
    assert seen["hidden"] == (0, 0, None)
    assert seen["shown"] == (217, 217)
    assert out == [FOCUS_LOST]  # 标签成了前台：提醒一次
    assert list(tmp_path.iterdir()) == []  # 没拖过：不写文件


def test_hidden_grip_moves_offscreen_and_is_placed_before_it_shows(monkeypatch, tmp_path):
    """隐藏时标签不只是透明，还移到屏幕外（不靠透明度 0 让鼠标穿过去）；再显示时先放回原位、
    再恢复不透明度，不会在屏幕外闪一下也不会在旧位置闪一下。"""
    tk = pytest.importorskip("tkinter")

    api = _fake_hearthstone(monkeypatch)
    window = _open_window(tk, out=lambda _l: None, position_path=tmp_path / "p.json")

    def scenario():
        grip = window._grip
        shown_at = grip.geometry
        api.foreground = 999  # 切到别的程序
        window._place()
        hidden = (grip.geometry, grip._top.geometry(), api.alpha[grip.hwnd])
        calls = []
        place, set_visible = grip.place, grip.set_visible
        grip.place = lambda rect: (calls.append(("place", rect)), place(rect))
        grip.set_visible = lambda v: (calls.append(("visible", v)), set_visible(v))
        api.foreground = 1
        window._place()
        return shown_at, hidden, calls, grip.geometry, api.alpha[grip.hwnd]

    shown_at, hidden, calls, back_at, alpha = _once_placed(window, scenario)
    geometry, tk_geometry, hidden_alpha = hidden
    assert geometry is None and tk_geometry.endswith("+-32000+-32000") and hidden_alpha == 0
    assert calls == [("place", shown_at), ("visible", True)]  # 先放回去，再显示
    assert back_at == shown_at and alpha == 217


def test_grip_style_is_checked_while_hidden(monkeypatch, tmp_path):
    """炉石不在前台时也检查标签的样式：掉了马上补上，而且标签留在屏幕外、透明。"""
    tk = pytest.importorskip("tkinter")
    from lscs.overlay.win32 import WS_EX_TRANSPARENT

    api = _fake_hearthstone(monkeypatch)
    window = _open_window(tk, out=lambda _l: None, position_path=tmp_path / "p.json")

    def scenario():
        grip = window._grip
        api.foreground = 999
        window._place()  # 隐藏
        api.styles[grip.hwnd] |= WS_EX_TRANSPARENT  # 样式被改了（比如 Tk 改了扩展样式）
        window._place()  # 还隐藏着
        return (
            api.styles[grip.hwnd] & WS_EX_TRANSPARENT,
            api.alpha[grip.hwnd],
            grip.geometry,
            grip._top.geometry().endswith("+-32000+-32000"),
            window._grip is grip,
        )

    assert _once_placed(window, scenario) == (0, 0, None, True, True)


def test_dragging_the_grip_moves_and_remembers_the_panel(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")
    from lscs.overlay import position
    from lscs.overlay.geometry import Anchor, Rect, anchor_from_rect

    _fake_hearthstone(monkeypatch)
    path = tmp_path / "p.json"
    out = []
    window = _open_window(tk, out=out.append, position_path=path)

    def drag(dx, dy):
        gx, gy = _grip_centre(window)
        window._grip_press(gx, gy)
        window._grip_motion(gx + dx // 2, gy + dy // 2)
        window._grip_motion(gx + dx, gy + dy)
        moved = window._placed[2]
        window._grip_release(gx + dx, gy + dy)
        return moved

    def scenario():
        seen = {"start": window._placed[2]}
        seen["tiny"] = (drag(2, -2), window._placed[2], path.exists())  # 手抖：不算拖
        seen["moved"] = drag(-200, -100)
        window._place()  # 之后的定位不弹回
        seen["after"] = window._placed[2]
        seen["file"] = position.load(path)
        seen["outside"] = drag(5000, 5000)  # 拖出客户区：夹回来
        seen["outside_file"] = position.load(path)
        window._grip_reset()  # 双击：回到预设
        seen["reset"] = (window._placed[2], path.exists())
        return seen

    seen = _once_placed(window, scenario)
    start = seen["start"]
    tiny, after_tiny, written = seen["tiny"]
    assert after_tiny == start and not written
    moved = seen["moved"]
    assert (moved.x, moved.y) == (start.x - 200, start.y - 100)
    assert seen["after"] == moved
    client = Rect(*CLIENT)
    assert seen["file"] == anchor_from_rect(moved, client)
    assert seen["file"].h == "right" and seen["file"].v == "bottom"
    outside = seen["outside"]
    assert (outside.right, outside.bottom) == (client.right, client.bottom)
    assert seen["outside_file"] == Anchor(1.0, 1.0, "right", "bottom")
    assert seen["reset"] == (start, False)
    assert out == [
        '面板位置已记住（p.json）；双击"≡ 拖动"恢复默认位置',  # 只在第一次保存时说
        "面板位置已恢复默认",
    ]


def test_drag_released_while_hearthstone_is_away_is_not_saved(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")

    api = _fake_hearthstone(monkeypatch)
    path = tmp_path / "p.json"
    window = _open_window(tk, out=lambda _l: None, position_path=path)

    def scenario():
        start = window._placed[2]
        gx, gy = _grip_centre(window)
        window._grip_press(gx, gy)
        window._grip_motion(gx - 300, gy - 300)
        api.foreground = 999  # 拖到一半切到了别的程序
        window._grip_motion(gx - 310, gy - 310)
        hidden = (window._visible, window._placed)
        window._grip_release(gx - 310, gy - 310)
        api.foreground = 1
        window._place()
        return start, hidden, window._drag, window._placed[2], path.exists()

    start, hidden, drag, box, written = _once_placed(window, scenario)
    assert hidden == (False, None)
    assert drag is None and box == start and not written


def test_drag_whose_release_was_lost_ends_on_the_place_tick(monkeypatch, tmp_path):
    """拖动中松手的消息没收到（比如弹出了系统对话框抢走了鼠标）：定位的定时器看到鼠标键已经
    松开、而且上次检查以来没有移动事件，就当作在最后的位置松手（照常保存）。"""
    tk = pytest.importorskip("tkinter")
    from lscs.overlay import position
    from lscs.overlay.geometry import Rect, anchor_from_rect
    from lscs.overlay.win32 import VK_LBUTTON

    api = _fake_hearthstone(monkeypatch)
    path = tmp_path / "p.json"
    window = _open_window(tk, out=lambda _l: None, position_path=path)

    def scenario():
        gx, gy = _grip_centre(window)
        api.keys[VK_LBUTTON] = 0x8000  # 按着
        window._grip_press(gx, gy)
        window._grip_motion(gx - 200, gy - 100)
        moved = window._placed[2]
        window._place_tick()
        window._place_tick()  # 还按着：继续拖
        still = (window._drag is not None, path.exists())
        window._grip_motion(gx - 210, gy - 110)
        api.keys[VK_LBUTTON] = 0  # 松开了，但 ButtonRelease 没来
        window._place_tick()  # 上次检查以来有移动：先不算
        waiting = window._drag is not None
        window._place_tick()
        return moved, still, waiting, window._drag, window._placed[2], position.load(path)

    moved, still, waiting, drag, box, saved = _once_placed(window, scenario)
    assert still == (True, False) and waiting
    assert drag is None
    assert (box.x, box.y) == (moved.x - 10, moved.y - 10)
    assert saved == anchor_from_rect(box, Rect(*CLIENT))


def test_lost_release_is_not_guessed_off_windows(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")
    import sys

    from lscs.overlay import win32

    if sys.platform == "win32":
        pytest.skip("只检查非 Windows")
    monkeypatch.setattr(win32, "IS_WINDOWS", False)
    window = _open_window(tk, out=lambda _l: None, position_path=tmp_path / "p.json")

    def scenario():
        gx, gy = _grip_centre(window)
        window._grip_press(gx, gy)
        window._grip_motion(gx - 50, gy + 50)
        window._place_tick()
        window._place_tick()
        dragging = window._drag is not None  # 不知道鼠标键的状态：等真的松手
        window._grip_release(gx - 50, gy + 50)
        return dragging, window._drag

    assert _once_placed(window, scenario) == (True, None)


def test_panel_starts_where_it_was_dragged_last_time(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")
    from lscs.overlay import position
    from lscs.overlay.geometry import Anchor

    _fake_hearthstone(monkeypatch)
    path = tmp_path / "p.json"
    position.save(path, Anchor(0.0625, 0.0556, "left", "top"))
    window = _open_window(tk, out=lambda _l: None, position_path=path)
    assert "显示在上次拖到的位置" in window.status
    box = _once_placed(window, lambda: window._placed[2])
    assert (box.x, box.y) == (100 + 100, 50 + 50)  # 客户区的 6.25% 宽、5.56% 高


def test_grip_on_mac_drags_through_tk_events_without_a_file(monkeypatch, tmp_path):
    """非 Windows：标签是普通置顶窗口，Tk 的绑定照样能拖；位置只在这次有效，不写文件。"""
    tk = pytest.importorskip("tkinter")
    import sys

    from lscs.overlay import win32

    if sys.platform == "win32":
        pytest.skip("只检查非 Windows")
    monkeypatch.setattr(win32, "IS_WINDOWS", False)
    out = []
    window = _open_window(tk, out=out.append, position_path=tmp_path / "p.json")
    assert '可以用右上角的"≡ 拖动"标签拖，位置不保存' in window.status
    assert window._grip is not None and window._grip.hwnd is None

    def scenario():
        start = window._placed[2]
        label = window._grip.label
        gx, gy = _grip_centre(window)
        label.event_generate("<ButtonPress-1>", x=5, y=5, rootx=gx, rooty=gy)
        label.event_generate("<B1-Motion>", x=5, y=5, rootx=gx - 150, rooty=gy + 80)
        label.event_generate("<ButtonRelease-1>", x=5, y=5, rootx=gx - 150, rooty=gy + 80)
        window._place()
        return start, window._placed, window._anchors[False], window._drag

    start, (_area, docked, box), anchor, drag = _once_placed(window, scenario)
    assert not docked and drag is None
    assert (box.x, box.y) == (start.x - 150, start.y + 80)
    assert anchor is not None
    assert list(tmp_path.iterdir()) == []
    assert out == []


def test_grip_that_cannot_be_styled_leaves_the_panel_alone(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")

    api = _fake_hearthstone(monkeypatch)
    real = api.SetLayeredWindowAttributes
    panel = []

    def only_the_panel(hwnd, key, alpha, flags):
        panel[:] = panel or [hwnd]  # 面板最先设置
        return real(hwnd, key, alpha, flags) if hwnd == panel[0] else 0

    api.SetLayeredWindowAttributes = only_the_panel
    window = _open_window(tk, out=lambda _l: None, position_path=tmp_path / "p.json")
    assert window.status.startswith("悬浮面板已打开：炉石在前台时显示在游戏窗口右下角")
    assert "面板不能拖动（SetLayeredWindowAttributes 失败（错误码 5））" in window.status
    assert window._grip is None
    width, height, x, y = _once_placed(window, lambda: window._geometry)
    assert (x + width, y + height) == (100 + 1568, 50 + 882)  # 面板照常贴在右下角
    assert window._native


def test_drag_handle_can_be_switched_off(monkeypatch, tmp_path):
    """[overlay] drag_handle = false：不开标签（万一点它会把炉石切到后台），面板照样用预设或
    上次拖到的位置。"""
    tk = pytest.importorskip("tkinter")
    from lscs.overlay import position
    from lscs.overlay.geometry import Anchor

    _fake_hearthstone(monkeypatch)
    path = tmp_path / "p.json"
    position.save(path, Anchor(0.0625, 0.0556, "left", "top"))
    try:
        window = _window_with(OverlayConfig(drag_handle=False), path)
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    assert window._grip is None
    assert (
        "显示在上次拖到的位置（删掉项目文件夹里的 overlay-position.json 恢复默认）" in window.status
    )
    assert "≡ 拖动" not in window.status and "不能拖动" not in window.status
    width, height, x, y = _once_placed(window, lambda: window._geometry)
    assert (x, y) == (100 + 100, 50 + 50)  # 没有标签：面板自己贴在记住的位置
    assert path.exists()


def test_drag_handle_off_on_mac_says_nothing_about_dragging(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")
    import sys

    from lscs.overlay import win32

    if sys.platform == "win32":
        pytest.skip("只检查非 Windows")
    monkeypatch.setattr(win32, "IS_WINDOWS", False)
    try:
        window = _window_with(OverlayConfig(drag_handle=False), tmp_path / "p.json")
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        assert window._grip is None
        assert window.status == "悬浮面板已打开（不是 Windows，放在屏幕右上角，不穿透鼠标）。"
    finally:
        window._release()


def _window_with(cfg, path):
    from lscs.overlay.window import OverlayWindow

    return OverlayWindow(cfg, out=lambda _l: None, position_path=path)


def test_grip_failure_while_running_only_stops_dragging(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")

    api = _fake_hearthstone(monkeypatch)
    out = []
    window = _open_window(tk, out=out.append, position_path=tmp_path / "p.json")

    def scenario():
        grip_hwnd = window._grip.hwnd
        real = api.SetLayeredWindowAttributes
        api.SetLayeredWindowAttributes = lambda h, k, a, f: (
            0 if h == grip_hwnd else real(h, k, a, f)
        )
        api.foreground = 999
        window._place()  # 隐藏：标签设不了透明度
        api.foreground = 1
        window._place()
        return window._grip, window._visible, api.alpha[window._hwnd]

    grip, visible, alpha = _once_placed(window, scenario)
    assert grip is None and visible and alpha == 217
    assert out == [
        "拖动标签出错（SetLayeredWindowAttributes 失败（错误码 5）），面板不能拖动了，其余照常。"
    ]


def test_focus_taken_by_the_grip_is_given_back(monkeypatch):
    tk = pytest.importorskip("tkinter")
    from lscs.overlay import grip as grip_module

    api = _fake_hearthstone(monkeypatch)
    api.foreground = 0x555  # 运行 watch 的 PowerShell
    real_init = grip_module.GripWindow.__init__

    def stealing(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        api.foreground = self.hwnd  # 第一次显示时抢走了焦点

    monkeypatch.setattr(grip_module.GripWindow, "__init__", stealing)
    window = _open_window(tk)
    try:
        assert api.focus_calls == [0x555]
    finally:
        window._release()


def test_pressing_the_grip_gives_focus_back_to_hearthstone(monkeypatch, tmp_path):
    """万一按标签时炉石被切到了后台（前台成了标签或面板）：按下和松开时都马上还给炉石，并在
    终端提醒一次（反复出现就关掉 drag_handle）。炉石本来就在前台时什么都不做。"""
    tk = pytest.importorskip("tkinter")

    api = _fake_hearthstone(monkeypatch)
    out = []
    window = _open_window(tk, out=out.append, position_path=tmp_path / "p.json")

    def scenario():
        gx, gy = _grip_centre(window)
        window._grip_press(gx, gy)  # 炉石在前台：不动焦点
        window._grip_release(gx, gy)
        quiet = (list(api.focus_calls), list(out))
        api.foreground = window._grip.hwnd
        window._grip_press(gx, gy)
        after_press = (api.foreground, list(api.focus_calls))
        api.foreground = window._hwnd
        window._grip_release(gx, gy)
        return quiet, after_press, api.foreground, list(api.focus_calls)

    quiet, after_press, foreground, calls = _once_placed(window, scenario)
    assert quiet == ([], [])
    assert after_press == (1, [1])  # 炉石的句柄是 1
    assert foreground == 1 and calls == [1, 1]
    assert out == [FOCUS_LOST]


def test_grip_reports_a_rebuilt_window(monkeypatch):
    """Tk 重建了标签的顶层窗口（新的句柄）：重新加上样式，并告诉面板要把它放回高亮上面。"""
    tk = pytest.importorskip("tkinter")
    from fake_win32 import FakeWin32Api

    from lscs.overlay.geometry import Rect
    from lscs.overlay.grip import GripWindow
    from lscs.overlay.win32 import WS_EX_NOACTIVATE, WS_EX_TRANSPARENT

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        root.withdraw()
        api = FakeWin32Api().install(monkeypatch)

        def nothing(*_args):
            pass

        grip = GripWindow(root, "TkDefaultFont", 0.85, True, nothing, nothing, nothing, nothing)
        assert grip.hwnd == 0xABC and api.alpha[0xABC] == 217
        assert grip.apply_style() is False
        api.styles[0xABC] |= WS_EX_TRANSPARENT  # 样式被别人改了：重设，但不算重建
        assert grip.apply_style() is False
        assert not api.styles[0xABC] & WS_EX_TRANSPARENT
        api.ancestor = 0xDEF  # Tk 重建了顶层窗口
        assert grip.apply_style() is True
        assert grip.hwnd == 0xDEF and api.styles[0xDEF] & WS_EX_NOACTIVATE
        grip.place(Rect(10, 20, 72, 22))
        grip.set_visible(False)
        assert api.alpha[0xDEF] == 0
        assert grip.geometry is None and grip._top.geometry().endswith("+-32000+-32000")
        grip.destroy()
    finally:
        root.destroy()


def test_position_that_cannot_be_saved_is_still_used(monkeypatch, tmp_path):
    tk = pytest.importorskip("tkinter")

    _fake_hearthstone(monkeypatch)
    out = []
    path = tmp_path / "missing" / "overlay-position.json"  # 文件夹不存在：写不进去
    window = _open_window(tk, out=out.append, position_path=path)

    def scenario():
        gx, gy = _grip_centre(window)
        window._grip_press(gx, gy)
        window._grip_motion(gx - 300, gy - 300)
        window._grip_release(gx - 300, gy - 300)
        moved = window._placed[2]
        window._place()
        return moved, window._placed[2]

    moved, after = _once_placed(window, scenario)
    assert after == moved  # 这次照样有效
    assert len(out) == 1 and out[0].startswith("面板位置无法保存：")
    assert not path.parent.exists()


def test_dragging_after_falling_back_to_a_plain_window_is_not_saved(monkeypatch, tmp_path):
    """贴靠炉石连续失败、退回普通置顶窗口：标签也变成普通窗口，拖动相对屏幕、不写文件。"""
    tk = pytest.importorskip("tkinter")

    api = _fake_hearthstone(monkeypatch)
    out = []
    path = tmp_path / "p.json"
    window = _open_window(tk, out=out.append, position_path=path)

    def broken():
        raise OSError("GetForegroundWindow 坏了")

    def scenario():
        api.GetForegroundWindow = broken
        for _ in range(5):
            window._place()
        grip = window._grip
        area, docked, start = window._placed
        gx, gy = _grip_centre(window)
        window._grip_press(gx, gy)
        window._grip_motion(gx - 100, gy + 100)
        window._grip_release(gx - 100, gy + 100)
        screen = window._screen()
        return grip, grip._native, docked, area == screen, start, window._placed[2], window._anchors

    grip, native, docked, on_screen, start, moved, anchors = _once_placed(window, scenario)
    assert grip is not None and not native and not docked
    assert on_screen  # 参照区域换成了屏幕
    assert (moved.x, moved.y) == (start.x - 100, start.y + 100)
    assert anchors[True] is None and anchors[False] is not None
    assert not path.exists()
    assert len(out) == 1 and out[0].startswith("贴靠炉石窗口连续 5 次失败")


@pytest.mark.parametrize(
    ("drag_handle", "expected"),
    [(True, "drag_handle 设为 false"), (False, "请告诉开发者")],
)
def test_focus_warning_only_mentions_drag_handle_when_the_grip_is_on(drag_handle, expected):
    """拖动标签已经关掉时，提醒里不再叫用户去关它。"""
    from types import SimpleNamespace

    from lscs.config import OverlayConfig
    from lscs.overlay.window import FOCUS_LOST_NO_GRIP, OverlayWindow

    out: list[str] = []
    fake = SimpleNamespace(
        _focus_warned=False, _out=out.append, _cfg=OverlayConfig(drag_handle=drag_handle)
    )
    OverlayWindow._note_focus_lost(fake)
    OverlayWindow._note_focus_lost(fake)  # 只提醒一次
    assert len(out) == 1 and expected in out[0]
    assert (out[0] == FOCUS_LOST_NO_GRIP) is (not drag_handle)
