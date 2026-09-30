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

## StandTerm font recommendations

`standterm-font-recommendations.js` measures independent xterm instances at the
active terminal's viewport size, renderer, and the draft font settings. It uses
FitAddon for columns/rows and divides the actual `.xterm-screen` size by those
counts for cell geometry. A guard against xterm 6.0.0's private renderer cell
metrics rejects deferred/stale layouts; reassess this guard on an xterm upgrade.
Measurement hosts stay inside the viewport but invisible, so IntersectionObserver
does not pause their renderer resize. Instances are disposed after measurement;
only one separate visual preview remains until settings close or change.

The candidate lists, wait bound, and reference aspect ratio live in this helper.
Capacity ranks by columns times rows; aspect ranks by distance from 2.0, then
capacity. Ties preserve the current font. Requested font names are not verified
as installed: fallback geometry is measured without requesting font permissions.
The settings goal and results are temporary. Selecting a result only updates the
font draft; the existing Save preferences path persists it and updates terminals
and Agent mirrors. Display/typography changes invalidate results instead of
automatically switching fonts. Preview glyphs help inspect fallback coverage and
geometry but are not an automated font quality check.
