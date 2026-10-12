@echo off
chcp 65001 >nul
cd /d "%~dp0"
where uv >nul 2>nul
if errorlevel 1 (
    echo 没有找到 uv。请先按 使用说明.md 的"第一次准备"安装 uv，装完关掉这个窗口再双击。
    pause
    exit /b 1
)
uv run lscs watch
echo.
echo lscs 已退出。可以关掉这个窗口了。
pause
