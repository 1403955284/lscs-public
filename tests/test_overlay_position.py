import json

import pytest

from lscs.config import PROJECT_ROOT
from lscs.overlay import position
from lscs.overlay.geometry import Anchor

DRAGGED = Anchor(0.0625, 0.9444, "left", "bottom")


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "overlay-position.json"
    position.save(path, DRAGGED)
    assert position.load(path) == DRAGGED
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "version": 1,
        "x": 0.0625,
        "y": 0.9444,
        "h": "left",
        "v": "bottom",
    }
    position.save(path, Anchor(1.0, 0.0, "right", "top"))  # 再拖一次：覆盖
    assert position.load(path) == Anchor(1.0, 0.0, "right", "top")
    assert [p.name for p in tmp_path.iterdir()] == ["overlay-position.json"]  # 没有 .tmp 残留


def test_missing_file_means_preset(tmp_path):
    assert position.load(tmp_path / "overlay-position.json") is None


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        "",
        "[]",
        '"right"',
        '{"version": 2, "x": 0.5, "y": 0.5, "h": "right", "v": "bottom"}',
        '{"version": true, "x": 0.5, "y": 0.5, "h": "right", "v": "bottom"}',
        '{"version": 1, "y": 0.5, "h": "right", "v": "bottom"}',
        '{"version": 1, "x": 1.5, "y": 0.5, "h": "right", "v": "bottom"}',
        '{"version": 1, "x": 0.5, "y": -0.1, "h": "right", "v": "bottom"}',
        '{"version": 1, "x": "0.5", "y": 0.5, "h": "right", "v": "bottom"}',
        '{"version": 1, "x": true, "y": 0.5, "h": "right", "v": "bottom"}',
        '{"version": 1, "x": NaN, "y": 0.5, "h": "right", "v": "bottom"}',
        '{"version": 1, "x": 0.5, "y": 0.5, "h": "middle", "v": "bottom"}',
        '{"version": 1, "x": 0.5, "y": 0.5, "h": "right", "v": "center"}',
    ],
)
def test_broken_file_means_preset(tmp_path, content):
    path = tmp_path / "overlay-position.json"
    path.write_text(content, encoding="utf-8")
    assert position.load(path) is None


def test_unreadable_file_means_preset(tmp_path):
    path = tmp_path / "overlay-position.json"
    path.write_bytes(b"\xff\xfe\x00 not utf-8")
    assert position.load(path) is None
    assert position.load(tmp_path) is None  # 是个文件夹（读取出错）


def test_whole_numbers_are_fine(tmp_path):
    path = tmp_path / "overlay-position.json"
    path.write_text('{"version": 1, "x": 1, "y": 0, "h": "center", "v": "top"}', encoding="utf-8")
    assert position.load(path) == Anchor(1.0, 0.0, "center", "top")


def test_clear_twice_is_fine(tmp_path):
    path = tmp_path / "overlay-position.json"
    position.save(path, DRAGGED)
    position.clear(path)
    assert not path.exists()
    position.clear(path)


def test_save_errors_reach_the_caller(tmp_path):
    with pytest.raises(OSError):
        position.save(tmp_path / "missing" / "overlay-position.json", DRAGGED)
    assert list(tmp_path.iterdir()) == []


def test_file_lives_in_the_project_folder_and_is_not_committed():
    assert position.DEFAULT_PATH.name == "overlay-position.json"
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "/overlay-position.json" in gitignore


def test_tests_never_touch_the_real_position_file():
    # conftest 把位置文件换成了临时目录（和 config.toml、reviews/ 一样）
    assert position.DEFAULT_PATH != PROJECT_ROOT / "overlay-position.json"
    assert PROJECT_ROOT not in position.DEFAULT_PATH.parents
