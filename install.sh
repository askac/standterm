#!/bin/bash
# StandTerm One-liner Installer
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/askac/standterm/main/install.sh | bash
#   or:
#   curl -fsSL https://raw.githubusercontent.com/askac/standterm/main/install.sh | bash -s -- --dir ~/standterm

set -e

REPO_URL="https://github.com/askac/standterm.git"
# Default to ./standterm under the current directory, matching the Windows
# one-line installer; STANDTERM_DIR and --dir still override.
INSTALL_DIR="${STANDTERM_DIR:-$(pwd)/standterm}"

# Allow --dir override
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dir|-d)
            if [[ -z "${2:-}" ]]; then
                echo "[!] ERROR: $1 requires a directory argument."
                exit 1
            fi
            INSTALL_DIR="$2"
            shift 2
            ;;
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
    echo "[*] python3 was not found; run.sh will try to guide or recover after install."
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

chmod +x "$INSTALL_DIR/run.sh"

echo "[+] Done. Launching StandTerm..."
echo ""
# Under `curl ... | bash` stdin is the pipe, so run.sh recovery prompts
# (e.g. installing python3 with apt) would auto-decline. Reattach the
# terminal when one is available so those prompts stay interactive.
if [ -r /dev/tty ]; then
    exec bash "$INSTALL_DIR/run.sh" </dev/tty
fi
exec bash "$INSTALL_DIR/run.sh"
