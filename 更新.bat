@echo off
chcp 65001 >nul
cd /d "%~dp0"
where git >nul 2>nul
if errorlevel 1 (
    echo 没有安装 git，不能自动更新。
    echo 请到 https://github.com/1403955284/lscs-public 点 Code - Download ZIP 重新下载，
    echo 解压到新文件夹，再把旧文件夹里的 config.toml 复制过去。
) else if not exist ".git" (
    echo 这个文件夹不是用 git 下载的，不能自动更新。
    echo 请到 https://github.com/1403955284/lscs-public 点 Code - Download ZIP 重新下载，
    echo 解压到新文件夹，再把旧文件夹里的 config.toml 复制过去。
) else (
    git pull
    if errorlevel 1 (
        echo.
        echo 更新失败。把上面的内容发给开发者。
    ) else (
        echo.
        echo 更新完成。下次双击 启动.bat 就是新版本。
    )
)
echo.
pause
