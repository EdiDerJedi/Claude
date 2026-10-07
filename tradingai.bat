@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Die Installation fehlt - bitte zuerst install.bat ausfuehren.
    pause
    exit /b 1
)
if "%~1"=="" (
    ".venv\Scripts\python.exe" -m tradingai --help
    echo.
    echo Beispiel:  tradingai.bat backtest --source yahoo
    pause
    exit /b 0
)
".venv\Scripts\python.exe" -m tradingai %*
exit /b %errorlevel%
