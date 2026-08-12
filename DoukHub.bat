@echo off
rem DoukHub tray launcher - no window, port cleanup by tray.py
cd /d "%~dp0"
start "" venv\Scripts\pythonw.exe tray.py
