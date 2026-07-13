@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo First run: creating venv...
    python -m venv venv
    venv\Scripts\python.exe -m pip install -r requirements.txt
)

REM ---------- Auto-kill old process on port 2999 ----------
echo Checking port 2999...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":2999" ^| findstr "LISTENING"') do (
    echo Killing old process on port 2999 ^(PID: %%a^)...
    taskkill /F /PID %%a >nul 2>&1
    timeout /t 2 /nobreak >nul
)

echo ================================
echo   DoukHub Starting...
echo   Local:  http://127.0.0.1:2999
echo   Press Ctrl+C to stop
echo ================================

start "" "http://127.0.0.1:2999"

venv\Scripts\python.exe main.py
pause
