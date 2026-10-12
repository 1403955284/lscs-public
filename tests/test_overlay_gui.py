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
    assert "第 3 回合 · 铸币 5 · 2 本 · 升本 6" in texts
    assert "规则" in texts and "AI" in texts
    assert any("① 买 海盗 3/2" in t for t in texts)
    assert any(t.startswith("目标：") for t in texts)
    assert "深度分析中…" in texts
    assert "AI（深度）" in texts and "要找：神明、飘荡祭品" in texts
    assert any(t.startswith("分析：畸变核心") for t in texts)
    assert "⚠ 状态可能不准" in texts
    assert "第 3 回合 · 战斗中" in texts
    # 招募时最下面的"对手"区：下一个对手就是最强威胁，合成一行
    assert "对手" in texts and any(t.startswith("▶★ 厄祖玛特 25+0") for t in texts)
    assert "第 3 回合 · 战斗中 · 对手 詹迪斯" in texts
    assert any(t.startswith("▶ 第8名 詹迪斯 已淘汰（残影）") for t in texts)
    assert "第8名 詹迪斯 已淘汰" not in texts
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
    assert "选英雄 · 护甲越少越强" in texts
    assert "★ 凯瑞尔·罗姆  护甲 10" in texts and "洛，在世传奇  护甲 19" in texts
    assert "分析中…" in texts
    assert any(t.startswith("✓ 也选 凯瑞尔·罗姆：") for t in texts)
    assert "泰瑟兰·血望者：要靠对子才强，前期慢" in texts
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
    assert texts[0].startswith("第 3 回合")  # 标题和警告总是保留（字大时标题可能换行）
    assert "AI（深度）" in texts
    if any(t.startswith("方向：") for t in texts):  # 精简时先去掉"方向"，再去掉"要找"
        assert any(t.startswith("要找：") for t in texts)
    assert "⚠ 状态可能不准" in texts or height == window._layout.max_height


def test_windows_path_with_fake_win32(monkeypatch):
    """在 Mac 上用假的 Win32 API 走一遍 Windows 的代码：贴靠、隐藏、连续失败后退回。"""
    tk = pytest.importorskip("tkinter")
    from fake_win32 import FakeWin32Api, FakeWindow

    from lscs.overlay.window import OverlayWindow

    hs = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"
    api = FakeWin32Api(
        windows=[FakeWindow(1, pid=10, size=(1600, 900), origin=(100, 50))], paths={10: hs}
    ).install(monkeypatch)
    api.foreground = 1  # 炉石在前台
    out = []
    try:
        window = OverlayWindow(OverlayConfig(), out=out.append)
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    assert window.status.startswith("悬浮面板已打开：炉石在前台时显示在游戏窗口右侧")
    model = OverlayModel(lambda c: c)
    model.on_update(STATE, [Event(type=EventType.RECRUIT_STARTED, turn=3)])
    seen = {}

    def broken():
        raise OSError("GetForegroundWindow 坏了")

    def keep_running():
        if "docked" not in seen and window._geometry is not None:
            seen["docked"] = window._geometry
            api.foreground = 999  # 切到别的程序
        elif "docked" in seen and "hidden" not in seen and api.alpha.get(0xABC) == 0:
            seen["hidden"] = True
            api.GetForegroundWindow = broken
        return not out

    window.run(model, keep_running)
    width, _height, x, y = seen["docked"]
    assert (x, y) == (100 + 1600 - 32 - width, 50 + 162)  # 客户区右侧 2%、顶部 18%
    assert width == 412  # 900 高 → 16 像素字 → 22 个字宽
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
    assert "▶ 等 AI 选…" in texts  # 没有目标的"下一步"
    assert "选择" in texts and "黑暗之赐 · 3 选 1" in texts
    assert "▶ 选 左数第 3 个 泰瑞尔" in texts and "选项 ○ ○ ●" in texts
    assert any("★泰瑞尔" in t for t in texts)
    assert any(t.startswith("32/32 直接成主力") for t in texts)
    assert snapshots[-1][0][1:3] == ["▶ 买 左数第 1 个 海盗", "商店 ●"]
    assert "选择" not in snapshots[-1][0]  # 选完：选择区消失
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
        assert window.shapes() == 3 + 2  # 三层描边 + 徽章的圆和字
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


def test_highlight_test_preview_window_draws_once(monkeypatch):
    pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay import calibrate, win32
    from lscs.overlay.layout import CALIBRATION_ROWS

    monkeypatch.setattr(win32, "IS_WINDOWS", False)  # 预览分支（Windows 上也只看几何）
    drawn = []
    real = calibrate.show_tk

    def show(frames, client, seconds, preview, cfg, focus):
        drawn.append((len(frames), preview))
        real(frames, client, seconds, preview, cfg, focus)

    out = []
    ok = calibrate.run_highlight_test(
        HighlightConfig(), 0.3, CALIBRATION_ROWS, out=out.append, show=show
    )
    if not ok and out[-1].startswith("× 无法打开高亮窗口"):
        pytest.skip(f"没有图形界面：{out[-1]}")
    assert ok, out
    assert drawn == [(29, True)]


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

        def show(self, step, client):
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


def test_highlight_badges_and_calibration_labels_are_drawn_differently():
    tk = pytest.importorskip("tkinter")
    from lscs.config import HighlightConfig
    from lscs.overlay.geometry import Rect
    from lscs.overlay.highlight import HighlightWindow
    from lscs.overlay.layout import Frame

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"没有图形界面：{exc}")
    try:
        root.withdraw()
        window = HighlightWindow(root, HighlightConfig(), preview=True)
        client = Rect(0, 0, 640, 360)
        canvas = window._canvas

        def drawn(frame):
            window.draw([frame], client)
            items = canvas.find_all()
            texts = [i for i in items if canvas.type(i) == "text"]
            ovals = [i for i in items if canvas.type(i) == "oval"]
            return canvas.coords(texts[0]), len(ovals)

        button = Rect(100, 100, 60, 60)
        # highlight-test 的两个字的按钮标签：写在圆的正中间，没有徽章
        assert drawn(Frame(button, round=True, label="升本")) == ([130.0, 130.0], 3)
        # "下一步"的徽章：框的左上角一个实心圆
        assert drawn(Frame(button, round=True, label="1", badge=True)) == ([100.0, 100.0], 4)
        slot = Rect(200, 50, 80, 100)
        assert drawn(Frame(slot, label="商店1"))[0] == [240.0, 100.0]
        window.destroy()
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

        def show(self, step, client):
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
