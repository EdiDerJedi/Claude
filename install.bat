@echo off
setlocal
cd /d "%~dp0"
title TradingAI - Installation
echo ============================================
echo   TradingAI - Installation
echo ============================================
echo.

set "PY="
call :finde_python
if not defined PY goto python_installieren

:python_ok
echo Verwende Python: %PY%
if exist ".venv\Scripts\python.exe" goto venv_ok
echo Erstelle die virtuelle Umgebung .venv ...
%PY% -m venv .venv
if errorlevel 1 goto fehler
:venv_ok

rem Eigener Temp-Ordner fuer pip: Manche Virenscanner sperren oder loeschen
rem Dateien im Windows-Temp-Ordner, waehrend pip sie installiert.
if not exist ".piptmp" mkdir ".piptmp"
set "TMP=%~dp0.piptmp"
set "TEMP=%~dp0.piptmp"

echo.
echo Installiere die benoetigten Pakete - das dauert ein paar Minuten ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 echo Hinweis: pip konnte nicht aktualisiert werden - es geht trotzdem weiter.

set /a VERSUCH=1
:pip_install
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not errorlevel 1 goto pip_ok
if %VERSUCH% GEQ 3 goto pip_fehler
set /a VERSUCH+=1
echo.
echo Die Installation wurde unterbrochen - starte Versuch %VERSUCH% von 3 ...
ping -n 6 127.0.0.1 >nul
goto pip_install

:pip_ok
if exist ".piptmp" rmdir /s /q ".piptmp" >nul 2>&1

if not exist "config.yaml" copy /y "config.example.yaml" "config.yaml" >nul
if exist "config.yaml" echo config.yaml ist vorhanden.

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


rem ---------------------------------------------------------------------
rem Sucht ein passendes Python: 3.13 oder 3.12 (64-Bit) bzw. 3.10 bis 3.14.
rem Neuere Versionen werden vom MetaTrader5-Paket evtl. noch nicht unterstuetzt.
:finde_python
py -3.13 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.13"
if defined PY exit /b 0
py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 set "PY=py -3.12"
if defined PY exit /b 0
python -c "import sys; sys.exit(0 if (3, 10) <= sys.version_info[:2] <= (3, 14) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if not errorlevel 1 set "PY=python"
if defined PY exit /b 0
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PY="%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
exit /b 0


:python_installieren
echo Es wurde kein passendes Python gefunden.
where winget >nul 2>&1
if errorlevel 1 goto python_manuell
echo Python 3.13 wird jetzt automatisch installiert - bitte warten ...
echo.
winget install -e --id Python.Python.3.13 --scope user --accept-package-agreements --accept-source-agreements
call :finde_python
if defined PY goto python_ok
echo.
echo Python wurde installiert, ist in diesem Fenster aber noch nicht bekannt.
echo Bitte dieses Fenster schliessen und install.bat noch einmal starten.
echo.
pause
exit /b 1

:python_manuell
echo.
echo Bitte installiere Python 3.13 [Windows installer 64-bit] von
echo     https://www.python.org/downloads/windows/
echo und setze im ersten Fenster des Installers den Haken bei "Add python.exe to PATH".
echo Danach diese Datei erneut starten.
echo.
pause
exit /b 1

:pip_fehler
echo.
echo ============================================================
echo   Die Pakete konnten auch nach 3 Versuchen nicht installiert werden.
echo ============================================================
echo Haeufigste Ursache: ein Virenscanner blockiert die Installation.
echo   - Virenscanner fuer 10 Minuten pausieren ODER diesen Ordner als
echo     Ausnahme eintragen:
echo     %~dp0
echo     [Windows-Sicherheit: Viren- und Bedrohungsschutz - Einstellungen
echo      verwalten - Ausschluesse - Ausschluss hinzufuegen - Ordner]
echo   - Danach install.bat erneut starten. Bereits geladene Pakete
echo     werden wiederverwendet.
echo Pruefe ausserdem die Internetverbindung.
echo.
pause
exit /b 1

:fehler
echo.
echo FEHLER bei der Installation - bitte die Meldungen oben lesen.
pause
exit /b 1
