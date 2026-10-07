@echo off
rem Startet das TradingAI-Dashboard ohne Konsolenfenster und oeffnet den Browser.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
    echo Die Installation fehlt - bitte zuerst install.bat ausfuehren.
    pause
    exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m tradingai dashboard
