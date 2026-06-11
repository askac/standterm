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
PYTHON_BOOTSTRAP=()

echo "========================================"
echo "   StandTerm Automated Starter ($PLATFORM_NAME)"
echo "========================================"

print_python_install_help() {
    echo "[!] ERROR: python3 is required but not found."
    echo "    Install with: sudo apt install python3 python3-venv python3-pip   (Debian/Ubuntu/WSL)"
    echo "                  brew install python3                      (macOS)"
    echo "    Or point the launcher at a manually prepared Python 3.10+ runtime:"
    echo "                  STANDTERM_PYTHON=/path/to/python3 ./run.sh --force"
    echo "    Or manually create the launcher venv, then rerun ./run.sh:"
    echo "                  $VENV_DIR"
}

ask_yes_no() {
    local prompt="$1"
    local answer
    if [[ ! -t 0 ]]; then
        return 1
    fi
    printf "%s [y/N] " "$prompt"
    read -r answer
    [[ "$answer" == "y" || "$answer" == "Y" || "$answer" == "yes" || "$answer" == "YES" ]]
}

try_install_system_python() {
    case "$PLATFORM_NAME" in
        WSL|Linux)
            if command -v apt-get >/dev/null 2>&1; then
                if ! ask_yes_no "[?] Install Python/venv packages with apt now?"; then
                    return 1
                fi
                if [[ "$(id -u)" -eq 0 ]]; then
                    apt-get update && apt-get install -y python3 python3-venv python3-pip
                elif command -v sudo >/dev/null 2>&1; then
                    sudo apt-get update && sudo apt-get install -y python3 python3-venv python3-pip
                else
                    echo "[!] sudo is not available; install python3 python3-venv python3-pip manually."
                    return 1
                fi
                return $?
            fi
            ;;
        macOS)
            if command -v brew >/dev/null 2>&1; then
                if ! ask_yes_no "[?] Install Python with Homebrew now?"; then
                    return 1
                fi
                brew install python3
                return $?
            fi
            ;;
    esac
    return 1
}

python_is_usable() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}

find_system_python3() {
    # Resolve python3 from PATH while skipping launcher venv shims. A stale
    # activated venv prepends its bin/ to PATH, so a bare "python3" lookup
    # would resolve to the broken venv interpreter and hide the system one.
    local candidate
    while IFS= read -r candidate; do
        case "$candidate" in
            "$PROJECT_DIR"/tools/.venv_*)
                continue
                ;;
        esac
        if python_is_usable "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done < <(type -ap python3 2>/dev/null)
    return 1
}

set_bootstrap_python() {
    if [[ -n "${STANDTERM_PYTHON:-}" ]]; then
        if python_is_usable "$STANDTERM_PYTHON"; then
            PYTHON_BOOTSTRAP=("$STANDTERM_PYTHON")
            return 0
        fi
        echo "[!] ERROR: STANDTERM_PYTHON is not a usable Python 3.10+ runtime: $STANDTERM_PYTHON"
        echo "    Set STANDTERM_PYTHON to a Python executable that supports venv, or manually create:"
        echo "    $VENV_DIR"
        return 1
    fi

    local system_python
    if system_python="$(find_system_python3)"; then
        PYTHON_BOOTSTRAP=("$system_python")
        return 0
    fi

    return 1
}

ensure_bootstrap_python() {
    if set_bootstrap_python; then
        return 0
    fi
    print_python_install_help
    if try_install_system_python && set_bootstrap_python; then
        return 0
    fi
    echo "[!] Cannot create or repair the virtual environment without a usable Python runtime."
    echo "    Manual fallback options:"
    echo "      1. Create the expected venv yourself: $VENV_DIR"
    echo "      2. Or rerun with STANDTERM_PYTHON=/path/to/python3 ./run.sh --force"
    return 1
}

remove_venv_dir() {
    case "$VENV_DIR" in
        "$PROJECT_DIR"/tools/.venv_*)
            rm -rf "$VENV_DIR"
            ;;
        *)
            echo "[!] ERROR: Refusing to remove unexpected venv path: $VENV_DIR"
            exit 1
            ;;
    esac
}

create_venv() {
    ensure_bootstrap_python || exit 1
    echo "[*] Creating virtual environment: $VENV_DIR..."
    mkdir -p "$PROJECT_DIR/tools"
    if "${PYTHON_BOOTSTRAP[@]}" -m venv "$VENV_DIR"; then
        return 0
    fi

    echo "[!] ERROR: Failed to create virtual environment."
    echo "[*] Removing incomplete virtual environment: $VENV_DIR"
    remove_venv_dir
    echo "    Debian/Ubuntu/WSL: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    echo "    macOS: brew install python3"

    if try_install_system_python; then
        echo "[*] Retrying virtual environment creation..."
        set_bootstrap_python && "${PYTHON_BOOTSTRAP[@]}" -m venv "$VENV_DIR" && return 0
    fi

    exit 1
}

ensure_venv_pip() {
    if python -m pip --version >/dev/null 2>&1; then
        return 0
    fi
    echo "[*] pip is missing in the virtual environment; attempting ensurepip..."
    python -m ensurepip --upgrade >/dev/null 2>&1 || {
        echo "[!] ERROR: pip is unavailable in the virtual environment."
        echo "    Debian/Ubuntu/WSL: sudo apt update && sudo apt install -y python3-venv python3-pip"
        echo "    Then rerun ./run.sh --force."
        return 1
    }
}

venv_activation_is_current() {
    local active_prefix
    active_prefix="$(python -c 'import sys; print(sys.prefix)' 2>/dev/null)" || return 1
    [ "$active_prefix" = "$VENV_DIR" ]
}

verify_dependencies() {
    python -c "import flask, flask_socketio, simple_websocket, paramiko, eventlet, cryptography, serial" >/dev/null 2>&1
}

hash_requirements() {
    if [ ! -f "$REQ_FILE" ]; then
        echo "missing"
        return
    fi
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$REQ_FILE" | awk '{print $1}'
    elif command -v shasum >/dev/null 2>&1; then
        shasum -a 256 "$REQ_FILE" | awk '{print $1}'
    else
        stat -c '%s:%Y' "$REQ_FILE" 2>/dev/null || stat -f '%z:%m' "$REQ_FILE"
    fi
}

expected_install_stamp() {
    printf 'platform=%s\n' "$PLATFORM_NAME"
    printf 'python=%s\n' "$(python -c 'import sys; print(sys.version.replace("\n", " "))')"
    printf 'requirements_sha256=%s\n' "$(hash_requirements)"
}

install_stamp_matches() {
    if [ ! -f "$INSTALLED_FLAG" ]; then
        return 1
    fi
    local expected
    expected="$(expected_install_stamp)"
    [ "$(cat "$INSTALLED_FLAG")" = "$expected" ]
}

# Check for force flag
FORCE_RECHECK=false
for arg in "$@"; do
    if [[ "$arg" == "--force" || "$arg" == "-f" ]]; then
        FORCE_RECHECK=true
    fi
done

# 1. Check and create virtual environment
if [ -d "$VENV_DIR" ] && [ ! -x "$VENV_DIR/bin/python" ]; then
    echo "[!] Existing virtual environment is incomplete: $VENV_DIR"
    ensure_bootstrap_python || exit 1
    echo "[*] Recreating it automatically..."
    remove_venv_dir
fi

if [ ! -d "$VENV_DIR" ]; then
    create_venv
    FORCE_RECHECK=true
fi

# 2. Activate virtual environment
echo "[*] Using Python virtual environment: $VENV_DIR"
echo "[*] Activating virtual environment..."
if [ ! -f "$VENV_DIR/bin/activate" ]; then
    echo "[!] Virtual environment activation script is missing."
    ensure_bootstrap_python || exit 1
    echo "[*] Recreating virtual environment automatically..."
    remove_venv_dir
    create_venv
    FORCE_RECHECK=true
fi
if ! source "$VENV_DIR/bin/activate"; then
    echo "[!] ERROR: Failed to activate virtual environment."
    ensure_bootstrap_python || exit 1
    echo "[*] Recreating virtual environment automatically..."
    remove_venv_dir
    create_venv
    FORCE_RECHECK=true
    source "$VENV_DIR/bin/activate" || exit 1
fi
if ! venv_activation_is_current; then
    echo "[!] Virtual environment activation points to a stale or unexpected Python prefix."
    ensure_bootstrap_python || exit 1
    echo "[*] Recreating virtual environment automatically..."
    remove_venv_dir
    create_venv
    FORCE_RECHECK=true
    source "$VENV_DIR/bin/activate" || exit 1
    venv_activation_is_current || exit 1
fi

echo "[*] Checking Python dependencies..."
if ! verify_dependencies; then
    echo "[*] Python dependencies are missing or unavailable; dependency check will run."
    FORCE_RECHECK=true
fi
if [ "$FORCE_RECHECK" = false ] && [ -f "$INSTALLED_FLAG" ] && ! install_stamp_matches; then
    echo "[*] Dependency stamp is stale; dependency check will run."
    FORCE_RECHECK=true
fi

# 3. Check and install dependencies
if [ "$FORCE_RECHECK" = true ] || ! install_stamp_matches; then
    rm -f "$INSTALLED_FLAG"
    ensure_venv_pip || exit 1
    if [ -f "$REQ_FILE" ]; then
        echo "[*] Installing/Updating dependencies from requirements.txt..."
        python -m pip install -q -r "$REQ_FILE"
    else
        echo "[!] WARNING: requirements.txt not found, installing basic packages..."
        python -m pip install -q Flask Flask-SocketIO simple-websocket paramiko eventlet cryptography pyserial
    fi

    if [ $? -eq 0 ] && verify_dependencies; then
        expected_install_stamp > "$INSTALLED_FLAG"
        echo "[+] Dependencies verified and flag created."
    else
        rm -f "$INSTALLED_FLAG"
        echo "[!] ERROR: Failed to install or verify dependencies."
        echo "    Fix the package error above, then rerun ./run.sh --force."
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
