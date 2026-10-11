"""检查并修复炉石的 log.config，确保 Power 日志写入文件并包含完整 tag。"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SECTION = "Power"
REQUIRED: dict[str, str] = {
    "LogLevel": "1",
    "FilePrinting": "true",
    "ConsolePrinting": "false",
    "ScreenPrinting": "false",
    "Verbose": "true",
}

# 炉石默认写到 10MB 就停止写 Power.log，而一局酒馆战棋约 33MB
CLIENT_CONFIG_SECTION = "Log"
CLIENT_CONFIG_REQUIRED: dict[str, str] = {"FileSizeLimit.Int": "-1"}


@dataclass(frozen=True)
class LogConfigCheck:
    ok: bool
    wrong: dict[str, str | None]  # 不符合要求的键 -> 当前值（缺失为 None）


def _parse_sections(text: str) -> list[tuple[str | None, list[str]]]:
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            sections.append((line[1:-1].strip(), []))
        else:
            sections[-1][1].append(raw)
    return sections


def _same_section(name: str | None, section: str) -> bool:
    return name is not None and name.lower() == section.lower()


def _values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in lines:
        if "=" in raw:
            key, value = raw.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def check_log_config(
    text: str, section: str = SECTION, required: dict[str, str] = REQUIRED
) -> LogConfigCheck:
    current: dict[str, str] = {}
    for name, lines in _parse_sections(text):
        if _same_section(name, section):
            current.update(_values(lines))
    wrong = {
        key: current.get(key)
        for key, want in required.items()
        if (current.get(key) or "").lower() != want
    }
    return LogConfigCheck(ok=not wrong, wrong=wrong)


def fix_log_config_text(
    text: str, section: str = SECTION, required: dict[str, str] = REQUIRED
) -> str:
    """保留其他段落原样；合并目标段并强制写入必需的键。"""
    merged: dict[str, str] = {}
    out: list[str] = []
    for name, lines in _parse_sections(text):
        if _same_section(name, section):
            merged.update(_values(lines))
            continue
        if name is not None:
            out.append(f"[{name}]")
        out.extend(lines)
    merged.update(required)
    while out and not out[-1].strip():
        out.pop()
    if out:
        out.append("")
    out.append(f"[{section}]")
    out.extend(f"{key}={value}" for key, value in merged.items())
    return "\n".join(out) + "\n"


def _fix_file(path: Path, section: str, required: dict[str, str]) -> bool:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if path.exists() and check_log_config(text, section, required).ok:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    path.write_text(fix_log_config_text(text, section, required), encoding="utf-8")
    return True


def fix_log_config(path: Path) -> bool:
    """修复 log.config，返回是否改动了文件。原文件备份为 log.config.bak。"""
    return _fix_file(path, SECTION, REQUIRED)


def fix_client_config(install_dir: Path) -> bool:
    """解除 Power.log 的 10MB 大小上限（写入安装目录下的 client.config）。"""
    return _fix_file(install_dir / "client.config", CLIENT_CONFIG_SECTION, CLIENT_CONFIG_REQUIRED)


def default_log_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        return base / "Blizzard" / "Hearthstone" / "log.config"
    return Path.home() / "Library" / "Preferences" / "Blizzard" / "Hearthstone" / "log.config"
