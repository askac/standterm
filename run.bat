@echo off
setlocal

set PROJECT_DIR=%~dp0
set VENV_DIR=%PROJECT_DIR%tools\.venv
set VENV_PYTHON=%VENV_DIR%\Scripts\python.exe
set APP_FILE=%PROJECT_DIR%app.py
set REQ_FILE=%PROJECT_DIR%requirements.txt
set INSTALLED_FLAG=%VENV_DIR%\.installed

echo ========================================
echo    StandTerm Automated Starter (Windows)
echo ========================================

python --version >nul 2>nul
if %errorlevel% neq 0 (
    echo [!] ERROR: python is required but not found.
    echo     Install Python 3.10+ from https://www.python.org/ and enable "Add python.exe to PATH".
    pause
    exit /b 1
)

:: 1. Check and create virtual environment
if not exist "%VENV_DIR%" (
    echo [*] Creating virtual environment: %VENV_DIR%...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [!] ERROR: Failed to create virtual environment.
        echo     Install or repair Python 3.10+ from https://www.python.org/
        echo     Make sure venv is available and python.exe is on PATH.
        pause
        exit /b 1
    )
    set FORCE_RECHECK=true
)

if not exist "%VENV_PYTHON%" (
    echo [!] ERROR: Virtual environment Python was not found: %VENV_PYTHON%
    echo     Delete "%VENV_DIR%" and rerun run.bat to recreate it.
    pause
    exit /b 1
)

:: 2. Activate virtual environment
call "%VENV_DIR%\Scripts\activate.bat"
if errorlevel 1 (
    echo [!] ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)

echo [*] Checking Python dependencies...
"%VENV_PYTHON%" -c "import flask, flask_socketio, simple_websocket, paramiko, eventlet, cryptography, serial, winpty" >nul 2>nul
if errorlevel 1 (
    echo [*] Python dependencies are missing or unavailable; dependency check will run.
    set FORCE_RECHECK=true
)

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
"%VENV_PYTHON%" -m pip install -q -r "%REQ_FILE%"
if %errorlevel% neq 0 (
    echo [!] ERROR: Failed to install dependencies.
    pause
    exit /b 1
)
"%VENV_PYTHON%" -c "import flask, flask_socketio, simple_websocket, paramiko, eventlet, cryptography, serial, winpty"
if errorlevel 1 (
    pause
    exit /b 1
)
echo [+] Dependencies verified.
type nul > "%INSTALLED_FLAG%"

:start
echo [*] Starting StandTerm server...
:: Run python with unbuffered output, then watch the first "Access URL:" line and
:: open it in the default browser on the first launch.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$o=$false; & '%VENV_PYTHON%' -u '%APP_FILE%' @args 2>&1 | ForEach-Object { Write-Host $_; if (-not $o -and $_ -match 'Access URL:\s*(\S+)') { Start-Process $matches[1]; $o=$true } }" %*

pause
