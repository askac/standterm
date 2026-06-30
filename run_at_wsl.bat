@echo off
setlocal

set "PROJECT_DIR=%~dp0"
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"
set "STANDTERM_RESTART_PATH=%~f0"
set "STANDTERM_RESTART_PATH=%STANDTERM_RESTART_PATH:\=/%"

echo ========================================
echo    StandTerm WSL Starter
echo ========================================

where wsl.exe >nul 2>nul
if errorlevel 1 (
    echo [!] ERROR: wsl.exe is required but was not found.
    echo     Install WSL, then rerun this script.
    pause
    exit /b 1
)

wsl.exe --cd "%PROJECT_DIR%" bash -lc "export STANDTERM_RESTART_PATH='%STANDTERM_RESTART_PATH%'; chmod +x ./run.sh; exec ./run.sh %*"
if errorlevel 1 (
    pause
    exit /b 1
)

pause
