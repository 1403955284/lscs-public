import pytest
from fake_win32 import FakeWin32Api, FakeWindow

from lscs.config import OverlayConfig
from lscs.overlay import check
from lscs.overlay.check import run_check
from lscs.overlay.win32 import WS_EX_TRANSPARENT

HS = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"


class FakeRoot:
    destroyed = False

    def __init__(self, hwnd=0x123):
        self.hwnd = hwnd

    def winfo_id(self):
        return self.hwnd

    def destroy(self):
        self.destroyed = True


@pytest.fixture(autouse=True)
def fake_grip(monkeypatch):
    """拖动标签那一步要在面板窗口上再开一个 Toplevel：换成假的。"""
    grips = []

    def make(_root):
        grips.append(FakeRoot(0x456))
        return grips[-1]

    monkeypatch.setattr(check, "_tk_grip", make)
    return grips


def windows_with_hearthstone(monkeypatch):
    api = FakeWin32Api(
        windows=[FakeWindow(1, pid=10, size=(1600, 900), origin=(8, 31))], paths={10: HS}
    ).install(monkeypatch)
    api.styles[api.ancestor] = 0x100
    return api


def test_all_steps_pass(monkeypatch, fake_grip):
    api = windows_with_hearthstone(monkeypatch)
    out, root = [], FakeRoot()
    assert run_check(OverlayConfig(), out=out.append, make_root=lambda: root) is True
    assert [line[:1] for line in out[:-1]] == ["√"] * 11
    assert "1600×900" in out[3]
    assert out[-3] == "√ 拖动标签：不穿透、不抢焦点、不进任务栏"
    assert out[-2].startswith("√ 卡图解码（Pillow）：Pillow ")
    assert root.destroyed and fake_grip[0].destroyed
    assert api.alpha[api.ancestor] == 217  # 检查完恢复成配置的不透明度
    assert out[-1].startswith("检查通过")


def test_grip_is_checked_on_its_own_window(monkeypatch):
    api = windows_with_hearthstone(monkeypatch)
    api.ancestor = None  # 面板和标签各有各的句柄
    api.styles[0x456] = WS_EX_TRANSPARENT  # 标签的穿透位要被清掉
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is True
    assert "√ 拖动标签：不穿透、不抢焦点、不进任务栏" in out
    assert api.styles[0x123] & WS_EX_TRANSPARENT  # 面板照样穿透
    assert not api.styles[0x456] & WS_EX_TRANSPARENT


def test_grip_failure_is_not_critical(monkeypatch, fake_grip):
    api = windows_with_hearthstone(monkeypatch)
    api.ancestor = None
    real = api.SetLayeredWindowAttributes

    def only_the_panel(hwnd, key, alpha, flags):
        return real(hwnd, key, alpha, flags) if hwnd == 0x123 else 0

    api.SetLayeredWindowAttributes = only_the_panel
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is True
    assert "× 拖动标签：SetLayeredWindowAttributes 失败（错误码 5）；面板能用，只是不能拖" in out
    assert fake_grip[0].destroyed
    assert out[-1].startswith("检查通过")


def test_grip_window_that_cannot_open_is_not_critical(monkeypatch):
    windows_with_hearthstone(monkeypatch)

    def no_toplevel(_root):
        raise RuntimeError("no toplevel")

    monkeypatch.setattr(check, "_tk_grip", no_toplevel)
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is True
    assert "× 拖动标签：no toplevel；面板能用，只是不能拖" in out


def test_failure_names_the_reason_and_fails(monkeypatch):
    api = windows_with_hearthstone(monkeypatch)
    api.layered_ok = False
    out, root = [], FakeRoot()
    assert run_check(OverlayConfig(), out=out.append, make_root=lambda: root) is False
    failed = [line for line in out if line.startswith("×")]
    assert failed[0] == "× 设置鼠标穿透和半透明：SetLayeredWindowAttributes 失败（错误码 5）"
    assert root.destroyed
    assert "--no-overlay" in out[-1]


def test_minimised_hearthstone_is_only_a_note(monkeypatch):
    # 全屏的炉石在切到 PowerShell 运行命令时会自动最小化，客户区是 0×0
    api = FakeWin32Api(windows=[FakeWindow(1, pid=10, size=(0, 0))], paths={10: HS}).install(
        monkeypatch
    )
    api.styles[api.ancestor] = 0x100
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is True
    line = next(line for line in out if "炉石窗口" in line)
    assert line == (
        "! 找炉石窗口：炉石现在是最小化状态：请切回炉石（或把炉石改成窗口模式）后再运行一次"
    )
    assert "UnityWndClass" not in "\n".join(out)


def test_hearthstone_not_running(monkeypatch):
    windows_with_hearthstone(monkeypatch).windows.clear()
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is False
    assert "× 找炉石窗口：没找到炉石的窗口：请先打开炉石再运行" in out


def test_hearthstone_not_found_explains_why(monkeypatch):
    FakeWin32Api(
        windows=[
            FakeWindow(1, pid=10, cls="SomeOtherClass"),
            FakeWindow(2, pid=10, size=(0, 0)),  # 最小化的 Unity 窗口不算"类名不对"
        ],
        paths={10: HS},
    ).install(monkeypatch)
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is True
    line = next(line for line in out if "炉石窗口" in line)
    assert line.startswith("!") and "最小化" in line

    FakeWin32Api(windows=[FakeWindow(1, pid=10, cls="SomeOtherClass")], paths={10: HS}).install(
        monkeypatch
    )
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is False
    line = next(line for line in out if "炉石窗口" in line)
    assert (
        line
        == "× 找炉石窗口：Hearthstone.exe 有窗口，但窗口类名是 SomeOtherClass，不是 UnityWndClass"
    )


def test_panel_creation_failure_skips_the_rest(monkeypatch):
    windows_with_hearthstone(monkeypatch)
    out = []

    def no_display():
        raise RuntimeError("no display")

    assert run_check(OverlayConfig(), out=out.append, make_root=no_display) is False
    assert "× 创建面板窗口：no display" in out
    assert sum(line.startswith("√") for line in out) == 5  # 前 4 项和 Pillow 照常
    assert any("跳过" in line for line in out)


def test_not_windows(monkeypatch):
    from lscs.overlay import win32

    monkeypatch.setattr(win32, "IS_WINDOWS", False)
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is False
    assert out[0].startswith("× 不是 Windows")


def test_missing_pillow_is_reported_but_not_critical(monkeypatch):
    windows_with_hearthstone(monkeypatch)

    def no_pillow():
        raise RuntimeError(check.PILLOW_MISSING)

    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot, pillow=no_pillow)
    assert "× 卡图解码（Pillow）：没有 Pillow：对手阵容只显示名字（运行 uv sync）" in out
    assert out[-1].startswith("检查通过")


def test_pillow_is_reported_with_its_version(monkeypatch):
    windows_with_hearthstone(monkeypatch)
    out = []
    pillow = lambda: "Pillow 10.4.0"  # noqa: E731
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot, pillow=pillow)
    assert "√ 卡图解码（Pillow）：Pillow 10.4.0" in out


def test_pillow_is_not_checked_when_opponent_boards_are_off(monkeypatch):
    windows_with_hearthstone(monkeypatch)
    out = []
    cfg = OverlayConfig(opponent_boards=False)
    assert run_check(cfg, out=out.append, make_root=FakeRoot)
    assert not any("Pillow" in line for line in out)


def test_default_pillow_check_reads_the_installed_version(monkeypatch):
    import sys

    import PIL

    assert check._pillow() == f"Pillow {PIL.__version__}"
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(RuntimeError, match="没有 Pillow"):
        check._pillow()
