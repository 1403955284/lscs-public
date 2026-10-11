"""查找炉石安装目录和 Power.log。"""

from __future__ import annotations

import sys
from pathlib import Path

INSTALL_CANDIDATES: dict[str, list[Path]] = {
    "win32": [
        Path(r"C:\Program Files (x86)\Hearthstone"),
        Path(r"C:\Program Files\Hearthstone"),
    ],
    "darwin": [Path("/Applications/Hearthstone")],
}


def find_install_dir(
    configured: Path | None = None, candidates: list[Path] | None = None
) -> Path | None:
    if configured is not None:
        return configured if configured.is_dir() else None
    if candidates is None:
        candidates = INSTALL_CANDIDATES.get(sys.platform, [])
    for path in candidates:
        if path.is_dir():
            return path
    return None


def find_power_logs(install_dir: Path) -> list[Path]:
    """返回安装目录下所有 Power.log，按修改时间从旧到新排序。"""
    logs = install_dir / "Logs"
    found = list(logs.glob("Hearthstone_*/Power.log"))
    legacy = logs / "Power.log"
    if legacy.is_file():
        found.append(legacy)
    return sorted(found, key=lambda p: p.stat().st_mtime)


def latest_power_log(install_dir: Path) -> Path | None:
    logs = find_power_logs(install_dir)
    return logs[-1] if logs else None
