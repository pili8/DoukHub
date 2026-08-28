@echo off
rem DoukHub tray launcher - no window, port cleanup by tray.py
cd /d "%~dp0"

if not exist "venv\Scripts\pythonw.exe" (
    echo First run: creating venv...
    python -m venv venv
    venv\Scripts\python.exe -m pip install -r requirements.txt
)

start "" venv\Scripts\pythonw.exe tray.py
