"""Windows 上双击用的 启动.bat / 更新.bat（AI 自我改正设计文档第 4 节）。在 Mac 上跑不了，
只检查内容和字节：UTF-8 无 BOM、CRLF、chcp 65001 之前只有 ASCII、括号块里的 echo 不含
cmd 的特殊字符、不用 goto 和标签。"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = ["启动.bat", "更新.bat"]
BOM = b"\xef\xbb\xbf"


def lines_of(name):
    data = (ROOT / name).read_bytes()
    assert not data.startswith(BOM)  # 有 BOM 时第一行会变成"'■@echo' 不是内部或外部命令"
    text = data.decode("utf-8")
    assert text.endswith("\r\n")
    assert "\n" not in text.replace("\r\n", "")  # 每一行都是 CRLF，没有单独的 LF 或 CR
    assert "\r" not in text.replace("\r\n", "")
    return text.split("\r\n")[:-1]


@pytest.mark.parametrize("name", LAUNCHERS)
def test_launcher_bytes_and_layout(name):
    lines = lines_of(name)
    assert lines[:3] == ["@echo off", "chcp 65001 >nul", 'cd /d "%~dp0"']
    assert all(line.isascii() for line in lines[:2])  # chcp 及之前的行按 GBK 读，只能是 ASCII
    assert "pause" in (line.strip() for line in lines)
    assert lines[-1] == "pause"  # 无论成功还是出错，窗口都留住
    for line in lines:
        assert not re.match(r"\s*goto\b", line, re.IGNORECASE)  # LF 文件里 cmd 有时找不到标签
        assert not line.lstrip().startswith(":")
        command = line.strip()
        if command.lower().startswith("echo"):
            text = command[4:]
            # 会提前结束括号块或被当成管道、重定向（中文全角标点可以随便用）
            assert not set(text) & set("()&|<>^"), line


def test_start_runs_watch_and_explains_a_missing_uv():
    lines = lines_of("启动.bat")
    assert "uv run lscs watch" in lines
    assert "where uv >nul 2>nul" in lines
    assert any("没有找到 uv" in line and "使用说明.md" in line for line in lines)


def test_update_pulls_or_explains_the_zip_download():
    text = "\n".join(lines_of("更新.bat"))
    assert "git pull" in text
    assert "Download ZIP" in text and "config.toml" in text
    assert 'if not exist ".git"' in text
    assert "https://github.com/1403955284/lscs-public" in text


def test_gitattributes_keeps_crlf_for_bat_files():
    rules = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "*.bat text eol=crlf" in rules


@pytest.mark.parametrize("name", LAUNCHERS)
def test_git_checks_bat_files_out_with_crlf(name):
    # 仓库里存的是 LF（text 会规范化行尾），检出时按 .gitattributes 转成 CRLF；
    # 不是 git 检出的目录（ZIP）里没有 .git，跳过
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("不是 git 检出的目录")
    out = subprocess.run(
        [git, "-c", "core.quotepath=off", "check-attr", "text", "eol", "--", name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert f"{name}: text: set" in out and f"{name}: eol: crlf" in out
