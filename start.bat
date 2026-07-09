@echo off
:: DoukHub 一键启动脚本 (Windows)
cd /d "%~dp0"

:: 检查虚拟环境
if not exist "venv" (
    echo 首次运行，正在初始化...
    python -m venv venv
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

echo ================================
echo   DoukHub 正在启动
echo   本机访问: http://127.0.0.1:2999
echo   局域网访问: 请用本机IP替代127.0.0.1
echo   按 Ctrl+C 退出
echo ================================

:: 自动打开浏览器
start "" "http://127.0.0.1:2999"

python main.py
pause
