@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "PROJECT_DIR=%~dp0"
set "TOOLS_DIR=%PROJECT_DIR%tools"
set "VENV_DIR=%TOOLS_DIR%\.venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "VENV_ACTIVATE=%VENV_DIR%\Scripts\activate.bat"
set "EMBED_PYTHON_DIR=%TOOLS_DIR%\python-embed"
set "EMBED_PYTHON=%EMBED_PYTHON_DIR%\python.exe"
set "APP_FILE=%PROJECT_DIR%app.py"
set "REQ_FILE=%PROJECT_DIR%requirements.txt"
set "EMBED_HELPER=%PROJECT_DIR%scripts\ensure_windows_python.ps1"
set "FORCE_RECHECK=false"
set "APP_ARGS="

echo ========================================
echo    StandTerm Automated Starter (Windows)
echo ========================================

for %%A in (%*) do (
    if "%%~A"=="--force" (
        set "FORCE_RECHECK=true"
    ) else if "%%~A"=="-f" (
        set "FORCE_RECHECK=true"
    ) else (
        set "APP_ARGS=!APP_ARGS! "%%~A""
    )
)

call :choose_python_runtime
if errorlevel 1 goto :fatal

call :ensure_dependencies
if errorlevel 1 goto :fatal

goto :start

:choose_python_runtime
python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [!] Python 3.10+ was not found on PATH.
    echo [*] Trying repo-local embedded Python fallback...
    call :ensure_embedded_python
    if errorlevel 1 exit /b 1
    exit /b 0
)

call :ensure_venv
if errorlevel 1 (
    echo [!] System Python could not create a working virtual environment.
    echo [*] Trying repo-local embedded Python fallback...
    call :ensure_embedded_python
    if errorlevel 1 exit /b 1
    exit /b 0
)

set "RUNTIME_PYTHON=%VENV_PYTHON%"
set "INSTALLED_FLAG=%VENV_DIR%\.installed"
set "RUNTIME_KIND=venv"
exit /b 0

:ensure_venv
if exist "%VENV_DIR%" if not exist "%VENV_PYTHON%" (
    echo [!] Existing virtual environment is incomplete: %VENV_DIR%
    echo [*] Recreating it automatically...
    rmdir /s /q "%VENV_DIR%" >nul 2>nul
)

if not exist "%VENV_DIR%" (
    echo [*] Creating virtual environment: %VENV_DIR%...
    python -m venv "%VENV_DIR%"
    if errorlevel 1 (
        echo [!] Failed to create virtual environment with system Python.
        echo     If Python was installed from the Microsoft Store, install the python.org build or use the embedded fallback.
        rmdir /s /q "%VENV_DIR%" >nul 2>nul
        exit /b 1
    )
    set "FORCE_RECHECK=true"
)

if not exist "%VENV_PYTHON%" (
    echo [!] Virtual environment Python was not found after creation: %VENV_PYTHON%
    rmdir /s /q "%VENV_DIR%" >nul 2>nul
    exit /b 1
)

if not exist "%VENV_ACTIVATE%" (
    echo [!] Virtual environment activation script is missing.
    rmdir /s /q "%VENV_DIR%" >nul 2>nul
    exit /b 1
)

call "%VENV_ACTIVATE%"
if errorlevel 1 (
    echo [!] Virtual environment activation failed.
    rmdir /s /q "%VENV_DIR%" >nul 2>nul
    exit /b 1
)

exit /b 0

:ensure_embedded_python
if not exist "%EMBED_HELPER%" (
    echo [!] ERROR: Embedded Python helper was not found: %EMBED_HELPER%
    echo     Install Python 3.10+ from https://www.python.org/ and enable "Add python.exe to PATH".
    exit /b 1
)

where powershell.exe >nul 2>nul
if errorlevel 1 (
    echo [!] ERROR: PowerShell is required to download embedded Python automatically.
    echo     Install Python 3.10+ from https://www.python.org/ and rerun run.bat.
    exit /b 1
)

set "PYTHON_OUT=%TEMP%\standterm_python_%RANDOM%%RANDOM%.txt"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%EMBED_HELPER%" -ProjectDir "%PROJECT_DIR%" -OutputPath "%PYTHON_OUT%"
if errorlevel 1 (
    del "%PYTHON_OUT%" >nul 2>nul
    echo [!] ERROR: Embedded Python fallback failed.
    echo     Manual options:
    echo       1. Install Python 3.10+ from https://www.python.org/ and enable "Add python.exe to PATH".
    echo       2. Or install with winget: winget install --id Python.Python.3.12 -e
    echo       3. Then reopen this terminal and rerun run.bat --force.
    exit /b 1
)

set /p "EMBEDDED_PYTHON="<"%PYTHON_OUT%"
del "%PYTHON_OUT%" >nul 2>nul
if not exist "%EMBEDDED_PYTHON%" (
    echo [!] ERROR: Embedded Python was not created correctly.
    echo     Delete "%EMBED_PYTHON_DIR%" and rerun run.bat, or install Python 3.10+ manually.
    exit /b 1
)

set "RUNTIME_PYTHON=%EMBEDDED_PYTHON%"
set "INSTALLED_FLAG=%EMBED_PYTHON_DIR%\.installed"
set "RUNTIME_KIND=embedded"
set "FORCE_RECHECK=true"
exit /b 0

:ensure_dependencies
echo [*] Using Python runtime: %RUNTIME_PYTHON%

"%RUNTIME_PYTHON%" -m pip --version >nul 2>nul
if errorlevel 1 (
    echo [*] pip is missing; attempting ensurepip...
    "%RUNTIME_PYTHON%" -m ensurepip --upgrade >nul 2>nul
    if errorlevel 1 (
        echo [!] ERROR: pip is unavailable for this Python runtime.
        if "%RUNTIME_KIND%"=="venv" (
            echo     Delete "%VENV_DIR%" and rerun run.bat, or install/repair Python from https://www.python.org/.
        ) else (
            echo     Delete "%EMBED_PYTHON_DIR%" and rerun run.bat, or install Python 3.10+ manually.
        )
        exit /b 1
    )
)

call :dependencies_available
if errorlevel 1 (
    echo [*] Python dependencies are missing or unavailable; dependency check will run.
    set "FORCE_RECHECK=true"
)

if "%FORCE_RECHECK%"=="false" if exist "%INSTALLED_FLAG%" (
    call :stamp_matches
    if errorlevel 1 (
        echo [*] Dependency stamp is stale; dependency check will run.
        set "FORCE_RECHECK=true"
    )
)

if "%FORCE_RECHECK%"=="true" goto :install_deps
if not exist "%INSTALLED_FLAG%" goto :install_deps

echo [*] Skipping dependency check (valid flag exists^).
echo [*] Hint: Use 'run.bat --force' to re-check.
exit /b 0

:install_deps
del "%INSTALLED_FLAG%" >nul 2>nul
if not exist "%REQ_FILE%" (
    echo [!] ERROR: requirements.txt was not found: %REQ_FILE%
    echo     Restore the repository files, then rerun run.bat.
    exit /b 1
)

echo [*] Installing/Updating dependencies...
"%RUNTIME_PYTHON%" -m pip install -q -r "%REQ_FILE%"
if errorlevel 1 (
    echo [!] ERROR: Failed to install dependencies.
    echo     Check network access and pip error messages, then rerun run.bat --force.
    exit /b 1
)

call :dependencies_available
if errorlevel 1 (
    echo [!] ERROR: Dependencies installed but verification still failed.
    echo     Try run.bat --force. If it still fails, delete "%VENV_DIR%" and "%EMBED_PYTHON_DIR%", then rerun.
    exit /b 1
)

call :write_stamp
echo [+] Dependencies verified and flag created.
exit /b 0

:dependencies_available
"%RUNTIME_PYTHON%" -c "import flask, flask_socketio, simple_websocket, paramiko, eventlet, cryptography, serial, winpty" >nul 2>nul
exit /b %errorlevel%

:stamp_matches
set "STAMP_TMP=%TEMP%\standterm_stamp_%RANDOM%%RANDOM%.txt"
call :write_stamp_to "%STAMP_TMP%"
fc /b "%INSTALLED_FLAG%" "%STAMP_TMP%" >nul 2>nul
set "STAMP_MATCH_RESULT=%errorlevel%"
del "%STAMP_TMP%" >nul 2>nul
exit /b %STAMP_MATCH_RESULT%

:write_stamp
call :write_stamp_to "%INSTALLED_FLAG%"
exit /b %errorlevel%

:write_stamp_to
set "STAMP_TARGET=%~1"
>"%STAMP_TARGET%" echo runtime=%RUNTIME_KIND%
"%RUNTIME_PYTHON%" -c "import sys; print('python=' + sys.version.replace(chr(10), ' '))" >>"%STAMP_TARGET%"
if exist "%REQ_FILE%" (
    for /f "tokens=1" %%H in ('certutil -hashfile "%REQ_FILE%" SHA256 ^| findstr /R "^[0-9A-Fa-f][0-9A-Fa-f]"') do (
        >>"%STAMP_TARGET%" echo requirements_sha256=%%H
        exit /b 0
    )
)
>>"%STAMP_TARGET%" echo requirements_sha256=missing
exit /b 0

:start
echo [*] Starting StandTerm server...
:: Run python with unbuffered output, then watch the first "Access URL:" line and
:: open it in the default browser on the first launch.
powershell -NoProfile -ExecutionPolicy Bypass -Command "$o=$false; & '%RUNTIME_PYTHON%' -u '%APP_FILE%' %APP_ARGS% 2>&1 | ForEach-Object { Write-Host $_; if (-not $o -and $_ -match 'Access URL:\s*(\S+)') { Start-Process $matches[1]; $o=$true } }"

pause
exit /b 0

:fatal
echo.
echo [!] StandTerm could not prepare a Python runtime automatically.
echo     Recommended manual recovery:
echo       1. Install Python 3.10+ from https://www.python.org/ and enable "Add python.exe to PATH".
echo       2. Reopen this terminal.
echo       3. Run: run.bat --force
pause
exit /b 1
