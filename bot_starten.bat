@echo off
cd /d "%~dp0"
title TradingAI - Bot laeuft - Fenster offen lassen, beenden mit Strg+C
echo Starte TradingAI ... der Start kann bis zu einer Minute dauern.
echo.
call "%~dp0tradingai.bat" run
echo.
echo Der Bot wurde beendet.
pause
