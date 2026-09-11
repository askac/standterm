# Vendored xterm.js Runtime

These browser bundles are vendored so StandTerm can run without loading terminal
assets from a CDN.

Current sources:

- `xterm.js`: `@xterm/xterm` 6.0.0
- `xterm-addon-unicode11.js`: `@xterm/addon-unicode11` 0.9.0
- `xterm-addon-webgl.js`: `@xterm/addon-webgl` 0.19.0
- `xterm-addon-fit.js`: `@xterm/addon-fit` 0.11.0
- `xterm-addon-web-links.js`: `@xterm/addon-web-links` 0.12.0

Source checkout:

- `/mnt/d/workspace/github/xterm.js`
- tag: `6.0.0`
- commit: `f447274f430fd22513f6adbf9862d19524471c04`

StandTerm carries one downstream change in `xterm-addon-webgl.js`: custom
Block Elements snap shared absolute octant boundaries to integer device pixels
when the glyph's used boundaries remain distinct, while unsafe axes retain
fractional coverage. This keeps composite quadrant glyphs seamless without
collapsing thin strokes or expanding intentional gaps. The change is proposed
upstream in xterm.js PR
[#6138](https://github.com/xtermjs/xterm.js/pull/6138); all other addon behavior
remains from the official 0.19.0 bundle.

The JavaScript bundles are copied from the npm package `lib/` output. The
matching stylesheet is copied to `../css/xterm.css` from `@xterm/xterm`.

xterm.js and its addons are MIT licensed. Keep the matching files under
`../licenses/` and the xterm.js section in
`../../THIRD-PARTY-NOTICES.md` when releasing these files.

## StandTerm IME positioning PoC

`standterm-ime-anchor-poc.js` is a separate experimental StandTerm adapter for
xterm 6.0.0, not a modification of the vendored `xterm.js` bundle. It anchors the
composition overlay and hidden textarea to the input line at composition start.
Text, selection, commit, and cancel handling remain owned by xterm. Resize,
buffer changes, and an unavailable/offscreen anchor restore native positioning
for the rest of that composition. A composition that begins at a temporary TUI
drawing cursor can still start in the wrong place.

The development build enables it through `IME_ANCHOR_POC_ENABLED` in
`templates/index.html`; set that constant to `false` and reload to compare with
native positioning. It uses guarded private APIs and must be reassessed on an
xterm upgrade. Synthetic browser checks do not qualify actual OS candidate
windows. The source checkout includes `docs/ime_anchor_poc.md` with manual checks.
