# Vendored xterm.js Runtime

These browser bundles are vendored so StandTerm can run without loading terminal
assets from a CDN.

Current sources:

- `xterm.js`: `@xterm/xterm` 6.0.0
- `xterm-addon-fit.js`: `@xterm/addon-fit` 0.11.0
- `xterm-addon-web-links.js`: `@xterm/addon-web-links` 0.12.0

Source checkout:

- `/mnt/d/workspace/github/xterm.js`
- tag: `6.0.0`
- commit: `f447274f430fd22513f6adbf9862d19524471c04`

The JavaScript bundles are copied from the npm package `lib/` output. The
matching stylesheet is copied to `../css/xterm.css` from `@xterm/xterm`.

xterm.js and its addons are MIT licensed. Keep
`../licenses/xtermjs-MIT-LICENSE.txt` and the xterm.js section in
`../../THIRD-PARTY-NOTICES.md` when releasing these files.
