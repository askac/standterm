#!/bin/bash

# StandTerm Startup Script for macOS / Linux / WSL

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_FILE="$PROJECT_DIR/app.py"
REQ_FILE="$PROJECT_DIR/requirements.txt"

detect_platform() {
    if [[ -n "${WSL_DISTRO_NAME:-}" ]]; then
        echo "WSL"
        return
    fi

    if [[ -r /proc/version ]] && grep -qi microsoft /proc/version; then
        echo "WSL"
        return
    fi

    case "$(uname -s)" in
        Darwin)
            echo "macOS"
            ;;
        Linux)
            echo "Linux"
            ;;
        *)
            echo "Unknown"
            ;;
    esac
}

PLATFORM_NAME="$(detect_platform)"

case "$PLATFORM_NAME" in
    WSL)
        VENV_DIR="$PROJECT_DIR/tools/.venv_wsl"
        ;;
    macOS)
        VENV_DIR="$PROJECT_DIR/tools/.venv_macos"
        ;;
    *)
        VENV_DIR="$PROJECT_DIR/tools/.venv_linux"
        ;;
esac

INSTALLED_FLAG="$VENV_DIR/.installed"
WIN_UART_VENV_DIR="$PROJECT_DIR/tools/.venv_win"

echo "========================================"
echo "   StandTerm Automated Starter ($PLATFORM_NAME)"
echo "========================================"

# Check for force flag
FORCE_RECHECK=false
for arg in "$@"; do
    if [[ "$arg" == "--force" || "$arg" == "-f" ]]; then
        FORCE_RECHECK=true
    fi
done

# 1. Check and create virtual environment
if [ ! -d "$VENV_DIR" ]; then
    echo "[*] Creating virtual environment: $VENV_DIR..."
    python3 -m venv "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "[!] ERROR: Failed to create virtual environment. Ensure python3-venv is installed."
        exit 1
    fi
    FORCE_RECHECK=true
fi

# 2. Activate virtual environment
echo "[*] Using Python virtual environment: $VENV_DIR"
echo "[*] Activating virtual environment..."
source "$VENV_DIR/bin/activate"

echo "[*] Checking Python dependencies..."
if ! python -c "import serial" >/dev/null 2>&1; then
    echo "[*] pyserial is missing or unavailable; dependency check will run."
    FORCE_RECHECK=true
fi
if ! python -c "import cryptography" >/dev/null 2>&1; then
    echo "[*] cryptography is missing or unavailable; dependency check will run."
    FORCE_RECHECK=true
fi

# 3. Check and install dependencies
if [ "$FORCE_RECHECK" = true ] || [ ! -f "$INSTALLED_FLAG" ]; then
    if [ -f "$REQ_FILE" ]; then
        echo "[*] Installing/Updating dependencies from requirements.txt..."
        pip install -q -r "$REQ_FILE"
    else
        echo "[!] WARNING: requirements.txt not found, installing basic packages..."
        pip install -q Flask Flask-SocketIO paramiko eventlet
    fi

    if [ $? -eq 0 ]; then
        touch "$INSTALLED_FLAG"
        echo "[+] Dependencies verified and flag created."
    else
        echo "[!] ERROR: Failed to install dependencies."
        exit 1
    fi
else
    echo "[*] Skipping dependency check (flag exists)."
    echo "[*] Hint: Use './run.sh --force' or delete '$INSTALLED_FLAG' to re-check."
fi

# 4. Start the server
ensure_wsl_windows_uart_helper() {
    if [[ "$PLATFORM_NAME" != "WSL" ]]; then
        return
    fi
    if ! command -v python.exe >/dev/null 2>&1; then
        echo "[*] WSL UART note: python.exe was not found; Windows COM bridge will be unavailable."
        return
    fi

    if [[ ! -x "$WIN_UART_VENV_DIR/Scripts/python.exe" ]]; then
        echo "[*] WSL UART note: creating Windows Python helper venv for COM bridge..."
        local win_uart_venv
        win_uart_venv="$(wslpath -w "$WIN_UART_VENV_DIR")"
        python.exe -m venv "$win_uart_venv" >/dev/null 2>&1 || {
            echo "[*] WSL UART note: failed to create Windows helper venv; install pyserial in Windows Python manually if needed."
            return
        }
    fi

    echo "[*] WSL UART note: checking Windows COM bridge dependencies..."
    if ! "$WIN_UART_VENV_DIR/Scripts/python.exe" -c "import serial" >/dev/null 2>&1; then
        echo "[*] WSL UART note: installing pyserial in Windows helper venv..."
        "$WIN_UART_VENV_DIR/Scripts/python.exe" -m pip install -q pyserial >/dev/null 2>&1 || {
            echo "[*] WSL UART note: failed to install pyserial in Windows helper venv."
            return
        }
    fi
}

if [[ "$PLATFORM_NAME" == "macOS" ]]; then
    echo "[*] macOS note: enable Remote Login if you want to SSH into localhost."
fi
if [[ "$PLATFORM_NAME" == "WSL" ]]; then
    echo "[*] WSL note: the browser will auto-open the WSL IP Access URL."
    echo "[*] WSL note: non-loopback access uses HTTPS by default."
    echo "[*] WSL note: WSL IP clients need browser authorization for Local Shell/UART unless STANDTERM_TRUST_WSL_CLIENT_IPS=1."
    echo "[*] WSL note: if browser authorization needs certificate trust, use the Authorizer details in the page."
fi

ensure_wsl_windows_uart_helper

open_browser() {
    local url="$1"
    case "$PLATFORM_NAME" in
        WSL)
            (cmd.exe /c start "" "$url" >/dev/null 2>&1 &) >/dev/null 2>&1
            ;;
        macOS)
            (open "$url" >/dev/null 2>&1 &) >/dev/null 2>&1
            ;;
        Linux)
            if command -v xdg-open >/dev/null 2>&1; then
                (xdg-open "$url" >/dev/null 2>&1 &) >/dev/null 2>&1
            fi
            ;;
    esac
}

echo "[*] Starting StandTerm server..."
echo "[*] Loading Python modules; first startup from /mnt/* may take a few seconds..."
# Run python with unbuffered output so we can detect the access URL line and open
# the browser once on the first launch.
python -u "$APP_FILE" "$@" 2>&1 | {
    browser_opened=
    while IFS= read -r line; do
        printf '%s\n' "$line"
        if [[ -z "$browser_opened" && "$line" == *"Access URL:"* ]]; then
            url="${line##*Access URL: }"
            url="${url%%[[:space:]]*}"
            browser_opened=1
            open_browser "$url"
        fi
    done
}
exit "${PIPESTATUS[0]}"
