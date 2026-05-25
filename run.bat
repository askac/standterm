@echo off
setlocal

set PROJECT_DIR=%~dp0
set VENV_DIR=%PROJECT_DIR%tools\.venv
set APP_FILE=%PROJECT_DIR%app.py
set REQ_FILE=%PROJECT_DIR%requirements.txt
set INSTALLED_FLAG=%VENV_DIR%\.installed

echo ========================================
echo    StandTerm Automated Starter (Windows)
echo ========================================

:: 1. Check and create virtual environment
if not exist "%VENV_DIR%" (
    echo [*] Creating virtual environment: %VENV_DIR%...
    python -m venv "%VENV_DIR%"
    if %errorlevel% neq 0 (
        echo [!] ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
    set FORCE_RECHECK=true
)

:: 2. Activate virtual environment
call "%VENV_DIR%\Scripts\activate.bat"

python -c "import serial" >nul 2>nul
if %errorlevel% neq 0 set FORCE_RECHECK=true
python -c "import cryptography" >nul 2>nul
if %errorlevel% neq 0 set FORCE_RECHECK=true

:: 3. Check and install dependencies
for %%A in (%*) do (
    if "%%~A"=="--force" set FORCE_RECHECK=true
    if "%%~A"=="-f" set FORCE_RECHECK=true
)

if "%FORCE_RECHECK%"=="true" goto :install
if not exist "%INSTALLED_FLAG%" goto :install

echo [*] Skipping dependency check (flag exists^).
echo [*] Hint: Use 'run.bat --force' to re-check.
goto :start

:install
echo [*] Installing/Updating dependencies...
pip install -q -r "%REQ_FILE%"
if %errorlevel% neq 0 (
    echo [!] ERROR: Failed to install dependencies.
    pause
    exit /b 1
)
echo [+] Dependencies verified.
type nul > "%INSTALLED_FLAG%"

:start
echo [*] Starting StandTerm server...
:: Run python with unbuffered output, then watch the first "Access URL:" line and
:: open it in the default browser on the first launch.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$o=$false; & python -u '%APP_FILE%' @args 2>&1 | ForEach-Object { Write-Host $_; if (-not $o -and $_ -match 'Access URL:\s*(\S+)') { Start-Process $matches[1]; $o=$true } }" %*

pause
