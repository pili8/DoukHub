@echo off
rem DoukHub 托盘启动(无黑窗口)
cd /d "%~dp0"
rem 清理旧进程(端口 2999)
for /f "tokens=5" %%a in ('netstat -aon ^| findstr /C:":2999 " ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)
rem 用 pythonw 无窗口启动托盘程序
start "" venv\Scripts\pythonw.exe tray.py