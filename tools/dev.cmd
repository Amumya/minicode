@echo off
rem 一键起 minicode 开发环境：Redis + 服务 + 窗口。
rem
rem 用法：双击这个文件，或者在任意终端里敲 tools\dev.cmd
rem 参数会原样传给 minicode dev，例如：
rem     tools\dev.cmd --port 8800
rem     tools\dev.cmd --no-redis
rem
rem 关掉窗口 / 按 Ctrl+C 就会把服务收掉（Redis 如果没被收干净，
rem 下次启动会自动复用，不会冲突）。

setlocal
cd /d "%~dp0.."

if not exist ".venv\Scripts\minicode.exe" (
    echo [x] 找不到 .venv\Scripts\minicode.exe
    echo     先装依赖：
    echo         python -m venv .venv
    echo         .venv\Scripts\python.exe -m pip install -e ".[dev]"
    pause
    exit /b 1
)

echo 启动中（Redis + 服务 + 窗口）...
.venv\Scripts\minicode.exe dev --build --open %*

echo.
echo 服务已停止。
pause
endlocal
