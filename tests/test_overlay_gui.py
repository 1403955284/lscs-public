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

NAMES = {"B": "海盗", "C": "蚀脑怪"}
STATE = make_state(turn=3, gold=5, tier=2, upgrade_cost=6, shop=[shop_minion(20, "B", 3, 2)])


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
        lambda: model.on_update(make_state(turn=3, phase=Phase.COMBAT, seq=3), []),
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
    assert any(t.startswith("思路：") for t in texts)
    assert "深度分析中…" in texts
    assert "AI（深度）" in texts and "要找：神明、飘荡祭品" in texts
    assert any(t.startswith("分析：畸变核心") for t in texts)
    assert "⚠ 状态可能不准" in texts
    assert "第 3 回合 · 战斗中" in texts
    assert snapshots[-1][0] == ["监听已停止，请查看终端"]
    full = next(size for shown, size in snapshots if "⚠ 状态可能不准" in shown)
    compact = snapshots[-1][1]
    assert compact[1] < full[1]  # 只有一行时面板变矮
    assert all(w > 0 and h > 0 for _, (w, h) in snapshots)
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
