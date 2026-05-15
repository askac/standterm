# WebSSH

A lightweight, web-based SSH terminal with advanced features, designed to run on **WSL2**, **Native Windows**, and **macOS**.

![WebSSH Demo](webssh_demo.gif)

## Features

- **Cross-Platform**:
  - **WSL2 Support**: Run and access your WSL terminal from a Windows browser.
  - **Native Windows Support**: Connect to any SSH server (including local OpenSSH) directly from Windows.
  - **macOS Support**: Run the server locally on macOS and connect to localhost or any reachable SSH server.
- **Professional UI & Themes**:
  - **Replicated Windows Terminal Themes**: Includes official color schemes from Microsoft Windows Terminal (Campbell, One Half Dark, Solarized, etc.).
  - **Vintage IBM 5153 Support**: Authentic CGA color palette for a classic CRT feel.
  - **256-color & True Color Support**: Full support for modern CLI applications.
- **Smart UX**:
  - **Select-to-Copy & Clear**: Professional terminal behavior where selection is automatically copied and cleared upon mouse release.
  - **Smart Key Auth**: Automatically attempts local SSH public key authentication for localhost targets, with optional key passphrase support from the password field.
  - **Draggable Context Menu**: Feature-rich menu with Paste, Google Search, and PiP options.
- **Advanced Capabilities**:
  - **URL Overlay**: Open URLs or image links in a resizable, draggable overlay window without leaving the terminal.
  - **Terminal PiP (Picture-in-Picture)**: Pop the terminal into a system-level floating window.
  - **Persistent Web Terminal Tabs**: Keep multiple terminal sessions alive across browser reloads while the WebSSH server process is still running.
- **Robustness**:
  - **Async Resource Loader**: Guaranteed startup stability with progress tracking for local browser assets.
  - **Anti-Crash Failsafes**: Graceful degradation if external addons fail to load.

## Prerequisites

- **Python 3.10+**
- **WSL2** (optional, for WSL mode)
- **OpenSSH Server** (for connecting to localhost)

## Quick Start

### One-liner Install (macOS / Linux / WSL)

```bash
curl -fsSL https://raw.githubusercontent.com/askac/webssh/main/install.sh | bash
```

### Manual Setup
1. Clone the repository.
2. Run `./run.sh` (Linux/macOS/WSL) or `run.bat` (Windows).
3. Open the generated URL in your browser.

### Localhost SSH Key Setup

If you want passwordless localhost login in WebSSH, your local SSH server must trust your public key.

```bash
mkdir -p ~/.ssh
chmod 700 ~/.ssh
cat ~/.ssh/id_ed25519.pub >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Verify it first with the system SSH client:

```bash
ssh 127.0.0.1
```

WebSSH uses your local private key for localhost targets. The server side must have the matching public key in `~/.ssh/authorized_keys`.

## Vendored Browser Assets

WebSSH vendors xterm.js runtime files under `static/` so the terminal works without a CDN:

- `@xterm/xterm` 6.0.0: `static/js/xterm.js`, `static/css/xterm.css`
- `@xterm/addon-fit` 0.11.0: `static/js/xterm-addon-fit.js`
- `@xterm/addon-web-links` 0.12.0: `static/js/xterm-addon-web-links.js`

The browser bundles are copied from the official npm release packages. A matching
source checkout is kept at `/mnt/d/workspace/github/xterm.js`, tag `6.0.0` /
commit `f447274f430fd22513f6adbf9862d19524471c04`, for auditing and future
upgrades.

xterm.js and these addons are MIT licensed. Keep `THIRD-PARTY-NOTICES.md`,
`static/licenses/xtermjs-MIT-LICENSE.txt`, and `static/js/README.md` when
publishing GitHub releases that include the vendored files.

## Acknowledgements & Copyright

This project utilizes color schemes and design patterns inspired by official terminal emulators:
- **xterm.js**: Browser terminal emulator and addons from the [xterm.js](https://github.com/xtermjs/xterm.js) project (MIT License).
- **Windows Terminal**: Color schemes (Campbell, Vintage, etc.) are replicated from the [Microsoft Windows Terminal](https://github.com/microsoft/terminal) project (MIT License).
- **IBM 5153**: Color palette based on the classic IBM 5153 Color Display.

## License

MIT - See `THIRD-PARTY-NOTICES.md` for external component licenses.
