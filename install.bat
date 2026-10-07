@echo off
setlocal
cd /d "%~dp0"
title TradingAI - Installation
echo ============================================
echo   TradingAI - Installation
echo ============================================
echo.

rem Passende Python-Version suchen: 3.13 oder 3.12 (64-Bit).
rem Neuere Versionen werden vom MetaTrader5-Paket evtl. noch nicht unterstuetzt.
set "PY="
py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.13"
if not defined PY (
    py -3.12 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PY=py -3.12"
)
if not defined PY (
    python -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) and sys.maxsize > 2**32 else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY goto kein_python

echo Verwende Python: %PY%
if not exist ".venv\Scripts\python.exe" (
    echo Erstelle die virtuelle Umgebung .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto fehler
)

echo.
echo Installiere die benoetigten Pakete - das dauert ein paar Minuten ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto fehler
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fehler

if not exist "config.yaml" (
    copy /y "config.example.yaml" "config.yaml" >nul
    echo config.yaml wurde aus config.example.yaml erstellt.
)

echo.
echo Pruefe die Installation ...
".venv\Scripts\python.exe" -c "import MetaTrader5, tradingai; print('OK - MetaTrader5-Paket Version', MetaTrader5.__version__)"
if errorlevel 1 goto fehler

echo.
echo ============================================
echo   Installation erfolgreich!
echo ============================================
echo Naechste Schritte:
echo   1. config.yaml im Editor anpassen
echo   2. Funktionstest:  tradingai.bat backtest --source synthetic --bars 3000
echo   3. Bot starten:    Doppelklick auf bot_starten.bat
echo.
pause
exit /b 0

:kein_python
echo Es wurde kein passendes Python gefunden.
echo.
echo Bitte installiere Python 3.13 [Windows installer 64-bit] von
echo     https://www.python.org/downloads/windows/
echo und setze im ersten Fenster des Installers den Haken bei "Add python.exe to PATH".
echo Danach diese Datei erneut starten.
echo.
pause
exit /b 1

:fehler
echo.
echo FEHLER bei der Installation - bitte die Meldungen oben lesen.
pause
exit /b 1
