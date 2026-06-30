@echo off
setlocal

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "SCREEN_NAME=standterm"
set "STANDTERM_RESTART_PATH=%~f0"
set "STANDTERM_RESTART_PATH=%STANDTERM_RESTART_PATH:\=/%"

echo ========================================
echo    StandTerm WSL Screen Starter
echo ========================================

where wsl.exe >nul 2>nul
if errorlevel 1 (
    echo [!] ERROR: wsl.exe is required but was not found.
    echo     Install WSL, then rerun this script.
    pause
    exit /b 1
)

wsl.exe --cd "%PROJECT_DIR%" bash -lc "command -v screen >/dev/null 2>&1"
if errorlevel 1 (
    echo [!] ERROR: screen is required inside WSL but was not found.
    echo     Install with: sudo apt install screen
    pause
    exit /b 1
)

echo [*] Starting or attaching WSL screen session: %SCREEN_NAME%
echo [*] Reattach from Windows with:
echo     wsl.exe screen -r standterm
echo [*] Force reattach with:
echo     wsl.exe screen -d -r standterm
wsl.exe --cd "%PROJECT_DIR%" bash -lc "export STANDTERM_RESTART_PATH='%STANDTERM_RESTART_PATH%'; chmod +x ./run.sh; screen -wipe standterm >/dev/null 2>&1 || true; if screen -ls | grep -Eq '[[:space:]][0-9]+\.standterm[[:space:]].*\(Detached\)'; then exec screen -r standterm; fi; if screen -ls | grep -Eq '[[:space:]][0-9]+\.standterm[[:space:]].*\(Attached\)'; then exec screen -d -r standterm; fi; exec screen -S standterm bash -lc 'exec ./run.sh %*'"
if errorlevel 1 (
    pause
    exit /b 1
)

echo [+] StandTerm screen session ended.
pause
