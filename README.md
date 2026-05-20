# WebSSH

A lightweight, web-based SSH terminal with advanced features, designed to run on **WSL2**, **Native Windows**, and **macOS**.

![WebSSH Demo](webssh_demo.gif)

## Features

- **Cross-Platform**:
  - **WSL2 Support**: Run and access your WSL terminal from a Windows browser through the WSL IP URL.
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
  - **Local Shell by Default**: Select Local Shell by default when the browser client is allowed to access host-local resources.
  - **Browser Authorization**: Enable Local Shell for non-loopback WSL browser clients with a WebCrypto browser key and an `authorized/` pairing file.
  - **UART Sessions**: Select a detected serial port or manually enter `COMx` / `/dev/...` and open it in a terminal tab.
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

### HTTPS for WSL Browser Authorization

When WebSSH listens on a non-loopback address, HTTPS is enabled by default.
This is required by modern browsers before WebCrypto browser authorization is
available from the WSL IP URL.

```bash
./run.sh
```

The Authorizer panel provides a WebSSH CA download link and pairing steps. Import
`webssh-local-ca.crt` into Windows Trusted Root Certification Authorities to
trust the generated WSL IP certificate. On WSL Windows-mounted paths, private
cert keys are stored under `~/.webssh/certs/...` by default so `chmod 600` is
effective. Set `WEBSSH_CERTS_DIR` to override that location.

Local Shell is selected by default when the browser is local or authorized, but
WebSSH does not automatically start a shell. Click **Connect to Local Shell** to
open the session.

To authorize a browser from the WSL IP URL:

1. Open the HTTPS Access URL printed by `run.sh`.
2. If the page is not trusted, download the WebSSH CA from the Authorizer details
   and import it into Windows Trusted Root Certification Authorities.
3. Click **Authorize** to download `webssh-authorize_*.json`.
4. Move that file into the repo-local `authorized/` directory.
5. Click **Check**.

Accepted browser keys are stored in `authorized/browsers.json`. Delete that file
or remove an entry to revoke access.

### UART Ports

Native Windows, macOS, and Linux use pyserial port discovery. WSL lists Windows
`COMx` ports through Windows APIs and bridges COM access through Windows Python
with pyserial; `run.sh` prepares `tools/.venv_win` automatically for that helper
when possible. If a port is not listed, enter it manually, such as `COM3`,
`/dev/ttyUSB0`, `/dev/ttyACM0`, or `/dev/cu.usbserial-0001`.

UART access follows the same local-client/browser-authorization gate as Local
Shell unless `WEBSSH_ALLOW_REMOTE_UART=1` is set.

### Debugging Policy State

Add `&debug=1` to the WebSSH URL to show an on-screen policy overlay with the
current default connection, selected mode, authorization state, and active tab
state. Set `WEBSSH_DEBUG_POLICY=1` before launching to print server-side policy
decisions.

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
