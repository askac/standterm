# Third-Party Notices

This project incorporates components from the following projects:

## xterm.js
- **License**: MIT
- **Copyright**: (c) 2017-2026 The xterm.js authors
- **Project**: https://github.com/xtermjs/xterm.js
- **Vendored files**: `static/js/xterm.js`, `static/js/xterm-addon-unicode11.js`, `static/js/xterm-addon-webgl.js`, `static/js/xterm-addon-fit.js`, `static/js/xterm-addon-web-links.js`, `static/js/xterm-addon-image.js`, `static/css/xterm.css`
- **Versions**: `@xterm/xterm` 6.0.0, `@xterm/addon-unicode11` 0.9.0, `@xterm/addon-webgl` 0.19.0, `@xterm/addon-fit` 0.11.0, `@xterm/addon-web-links` 0.12.0, `@xterm/addon-image` 0.9.0
- **Modification**: StandTerm pixel-aligns custom WebGL Block Element rectangles to avoid seams at odd device-cell widths.
- **Image addon modification**: StandTerm consumes failed iTerm2 bitmap decodes, releases fallback object URLs on completion/error/timeout, and discards pending decodes after addon disposal.
- **License texts**: `static/licenses/xtermjs-MIT-LICENSE.txt`, `static/licenses/xterm-addon-webgl-MIT-LICENSE.txt`, `static/licenses/xterm-addon-image-MIT-LICENSE.txt`

## SIXEL and xterm-wasm-parts
- **License**: MIT
- **Copyright**: Joerg Breitbart
- **Projects**: https://github.com/jerch/sixel, https://github.com/jerch/xterm-wasm-parts
- **Notice**: The image addon includes the SIXEL decoder and the xterm-wasm-parts base64 decoder. See the addon's npm package for source and dependency ranges.
- **License texts**: `static/licenses/sixel-MIT-LICENSE.txt`, `static/licenses/xterm-wasm-parts-MIT-LICENSE.txt`

## Socket.IO
- **License**: MIT
- **Copyright**: (c) 2014-2023 Guillermo Rauch
- **Project**: https://github.com/socketio/socket.io

## Powerline Symbols
- **License**: MIT
- **Copyright**: Copyright 2013 Kim Silkebækken and other contributors
- **Project**: https://github.com/powerline/powerline
- **Vendored file**: `static/fonts/PowerlineSymbols.otf`
- **Upstream commit**: `11808cbe5c16e4621edcaefa916b5add81eab799`
- **License text**: `static/licenses/powerline-MIT-LICENSE.txt`

## Microsoft Windows Terminal (Color Schemes)
- **License**: MIT
- **Copyright**: (c) Microsoft Corporation
- **Project**: https://github.com/microsoft/terminal
- **Notice**: Color schemes (Campbell, Vintage, One Half Dark, etc.) are replicated from the Windows Terminal project to provide a consistent user experience.

## IBM 5153 Color Palette
- **Notice**: The IBM 5153 (CGA) color palette is a reproduction of the classic hardware color specifications for educational and aesthetic purposes.
