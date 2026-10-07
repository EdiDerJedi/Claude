@echo off
cd /d "%~dp0"
title TradingAI - Bot laeuft - Fenster offen lassen, beenden mit Strg+C
call "%~dp0tradingai.bat" run
echo.
echo Der Bot wurde beendet.
pause
