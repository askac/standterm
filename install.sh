#!/bin/bash
# StandTerm One-liner Installer
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/askac/standterm/main/install.sh | bash
#   or:
#   curl -fsSL https://raw.githubusercontent.com/askac/standterm/main/install.sh | bash -s -- --dir ~/standterm

set -e

REPO_URL="https://github.com/askac/standterm.git"
INSTALL_DIR="${STANDTERM_DIR:-$HOME/standterm}"

# Allow --dir override
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir|-d) INSTALL_DIR="$2"; shift 2 ;;
        *) shift ;;
    esac
done

echo "========================================"
echo "   StandTerm Installer"
echo "========================================"

# Check dependencies
if ! command -v git &>/dev/null; then
    echo "[!] ERROR: git is required but not found."
    echo "    Install with: sudo apt install git   (Debian/Ubuntu/WSL)"
    echo "                  brew install git        (macOS)"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    echo "[!] ERROR: python3 is required but not found."
    echo "    Install with: sudo apt install python3 python3-venv   (Debian/Ubuntu/WSL)"
    echo "                  brew install python3                      (macOS)"
    exit 1
fi

# Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[*] Existing installation found at: $INSTALL_DIR"
    echo "[*] Pulling latest changes..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    echo "[*] Installing to: $INSTALL_DIR"
    git clone "$REPO_URL" "$INSTALL_DIR"
fi

echo "[+] Done. Launching StandTerm..."
echo ""
exec bash "$INSTALL_DIR/run.sh"
