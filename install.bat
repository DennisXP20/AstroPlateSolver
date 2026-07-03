@echo off
cd /d "%~dp0"
setlocal

echo ==========================================
echo   Astro Plate Solver - Installation
echo ==========================================
echo.
echo Suche Python...

:: 1) python im PATH?
python --version >nul 2>&1
if not errorlevel 1 (
    set "PY=python"
    goto FOUND
)

:: 2) py-Launcher (Standard bei python.org-Installationen)?
py -3 --version >nul 2>&1
if not errorlevel 1 (
    set "PY=py -3"
    goto FOUND
)

:: 3) Typische Installationspfade pruefen
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "C:\Python313\python.exe"
    "C:\Python312\python.exe"
    "C:\Python311\python.exe"
    "C:\Python310\python.exe"
) do (
    if exist %%P (
        set "PY=%%~P"
        goto FOUND
    )
)

echo [FEHLER] Python nicht gefunden!
echo.
echo Bitte Python 3.10 oder neuer installieren:
echo   https://www.python.org/downloads/
echo.
echo WICHTIG: Beim Installieren "Add Python to PATH" ankreuzen!
pause
exit /b 1

:FOUND
echo Python gefunden:
%PY% --version
echo.
echo Installiere Pakete (numpy, scipy, astropy, pillow, certifi, python-docx)...
echo.
%PY% -m pip install --upgrade pip --quiet
%PY% -m pip install numpy scipy astropy pillow certifi python-docx --upgrade
if errorlevel 1 (
    echo.
    echo [FEHLER] Paket-Installation fehlgeschlagen.
    echo Versuche manuell in der Eingabeaufforderung:
    echo   python -m pip install numpy scipy astropy pillow certifi python-docx
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   Installation erfolgreich!
echo ==========================================
echo.
echo Naechste Schritte:
echo   1. start.bat doppelklicken - der Server startet auf http://localhost:8743
echo   2. Im Browser den Objektkatalog herunterladen (einmalig, ~500 MB)
echo   3. Optional: ASTAP installieren fuer Plate Solving ohne Vorwissen
echo      https://www.hnsky.org/astap.htm  (+ Sternkatalog D80 oder H17/H18)
echo.
pause
