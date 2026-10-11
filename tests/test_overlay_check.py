from fake_win32 import FakeWin32Api, FakeWindow

from lscs.config import OverlayConfig
from lscs.overlay.check import run_check

HS = r"C:\Program Files (x86)\Hearthstone\Hearthstone.exe"


class FakeRoot:
    destroyed = False

    def winfo_id(self):
        return 0x123

    def destroy(self):
        self.destroyed = True


def windows_with_hearthstone(monkeypatch):
    api = FakeWin32Api(
        windows=[FakeWindow(1, pid=10, size=(1600, 900), origin=(8, 31))], paths={10: HS}
    ).install(monkeypatch)
    api.styles[api.ancestor] = 0x100
    return api


def test_all_steps_pass(monkeypatch):
    api = windows_with_hearthstone(monkeypatch)
    out, root = [], FakeRoot()
    assert run_check(OverlayConfig(), out=out.append, make_root=lambda: root) is True
    assert [line[:1] for line in out[:-1]] == ["√"] * 9
    assert "1600×900" in out[3]
    assert root.destroyed
    assert api.alpha[api.ancestor] == 217  # 检查完恢复成配置的不透明度
    assert out[-1].startswith("检查通过")


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
    assert sum(line.startswith("√") for line in out) == 4  # 前 4 项照常
    assert any("跳过" in line for line in out)


def test_not_windows(monkeypatch):
    from lscs.overlay import win32

    monkeypatch.setattr(win32, "IS_WINDOWS", False)
    out = []
    assert run_check(OverlayConfig(), out=out.append, make_root=FakeRoot) is False
    assert out[0].startswith("× 不是 Windows")
