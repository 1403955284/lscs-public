import os

from lscs.logwatch.paths import find_install_dir, find_power_logs, latest_power_log


def _touch(path, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    os.utime(path, (mtime, mtime))


def test_find_install_dir_prefers_configured(tmp_path):
    assert find_install_dir(tmp_path) == tmp_path
    assert find_install_dir(tmp_path / "missing") is None


def test_find_install_dir_uses_first_existing_candidate(tmp_path):
    existing = tmp_path / "b"
    existing.mkdir()
    assert find_install_dir(candidates=[tmp_path / "a", existing]) == existing
    assert find_install_dir(candidates=[tmp_path / "a"]) is None


def test_power_logs_sorted_by_mtime_and_latest(tmp_path):
    old = tmp_path / "Logs" / "Hearthstone_2026_10_01_10_00_00" / "Power.log"
    new = tmp_path / "Logs" / "Hearthstone_2026_10_02_10_00_00" / "Power.log"
    _touch(new, 2000)
    _touch(old, 1000)
    assert find_power_logs(tmp_path) == [old, new]
    assert latest_power_log(tmp_path) == new


def test_legacy_power_log_location_is_found(tmp_path):
    legacy = tmp_path / "Logs" / "Power.log"
    _touch(legacy, 1000)
    assert latest_power_log(tmp_path) == legacy


def test_no_logs(tmp_path):
    assert find_power_logs(tmp_path) == []
    assert latest_power_log(tmp_path) is None
