@echo off
cd /d "%~dp0"
setlocal

:: Python finden (PATH, dann py-Launcher)
set "PY="
python --version >nul 2>&1
if not errorlevel 1 set "PY=python"
if not defined PY (
    py -3 --version >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
)
if not defined PY (
    echo [FEHLER] Python nicht gefunden!
    echo.
    echo Bitte zuerst install.bat ausfuehren oder Python installieren:
    echo   https://www.python.org/downloads/
    echo WICHTIG: "Add Python to PATH" ankreuzen!
    echo.
    pause
    exit /b 1
)

:: Alte Server-Instanz auf Port 8743 beenden
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr "127.0.0.1:8743"') do (
    if not "%%a"=="0" taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

echo Server laeuft auf http://localhost:8743
echo Zum Beenden dieses Fenster schliessen.
echo.
start "" http://localhost:8743
%PY% server.py
pause
