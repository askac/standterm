(function (root) {
    'use strict';

    const FONT_WAIT_MS = 2000;
    const ASPECT_TARGET = 2;
    const WINDOWS_CANDIDATES = [
        'Consolas, "Cascadia Mono", "Courier New", monospace',
        '"Cascadia Mono", Consolas, "Courier New", monospace',
        '"Courier New", monospace'
    ];
    const APPLE_CANDIDATES = [
        'Menlo, Monaco, "SF Mono", monospace',
        'Monaco, Menlo, monospace',
        '"SF Mono", Menlo, Monaco, monospace'
    ];

    function bounded(promise, signal) {
        return new Promise((resolve, reject) => {
            const abort = () => finish(reject, new Error('Font evaluation cancelled'));
            const timer = setTimeout(() => finish(reject, new Error('Font evaluation timed out')), FONT_WAIT_MS);
            function finish(callback, value) {
                clearTimeout(timer);
                signal.removeEventListener('abort', abort);
                callback(value);
            }
            signal.addEventListener('abort', abort, { once: true });
            if (signal.aborted) abort();
            Promise.resolve(promise).then(value => finish(resolve, value), error => finish(reject, error));
        });
    }

    function nextLayout(signal) {
        return bounded(new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))), signal);
    }

    async function openPreview(host, options, renderer, signal, onContextLoss) {
        let term = null;
        let webgl = null;
        let disposed = false;
        const dispose = () => {
            if (disposed) return;
            disposed = true;
            signal.removeEventListener('abort', dispose);
            if (term) term.dispose();
            host.replaceChildren();
        };
        try {
            if (document.fonts) {
                await bounded(document.fonts.load(`${options.fontWeight} ${options.fontSize}px ${options.fontFamily}`, 'WMi'), signal);
                await bounded(document.fonts.ready, signal);
            }
            if (signal.aborted) throw new Error('Font evaluation cancelled');
            term = new root.Terminal({ ...options, cursorBlink: false, disableStdin: true });
            signal.addEventListener('abort', dispose, { once: true });
            const fit = new root.FitAddon.FitAddon();
            term.loadAddon(fit);
            term.open(host);
            if (renderer === 'webgl') {
                webgl = new root.WebglAddon.WebglAddon(true);
                webgl.onContextLoss(() => {
                    dispose();
                    onContextLoss();
                });
                term.loadAddon(webgl);
            }
            await nextLayout(signal);
            if (disposed) throw new Error('Font preview unavailable');
            const proposed = fit.proposeDimensions();
            if (!proposed || !Number.isInteger(proposed.cols) || !Number.isInteger(proposed.rows)
                || proposed.cols < 2 || proposed.rows < 1) throw new Error('Font geometry unavailable');
            fit.fit();
            await nextLayout(signal);
            if (disposed) throw new Error('Font preview unavailable');
            return { term, fit, dispose };
        } catch (error) {
            if (webgl && !disposed) webgl.dispose();
            dispose();
            throw error;
        }
    }

    async function measure(options, viewport, renderer, signal) {
        const host = document.createElement('div');
        host.className = 'font-measurement-host';
        host.inert = true;
        host.style.width = `${viewport.width}px`;
        host.style.height = `${viewport.height}px`;
        host.setAttribute('aria-hidden', 'true');
        document.body.appendChild(host);
        let preview = null;
        try {
            preview = await openPreview(host, options, renderer, signal, () => {});
            const screen = host.querySelector('.xterm-screen').getBoundingClientRect();
            const { cols, rows } = preview.term;
            const cellWidth = screen.width / cols;
            const cellHeight = screen.height / rows;
            // xterm 6.0.0 can defer a renderer resize while not intersecting the viewport.
            // Check its bounded private metrics, also used by the vendored FitAddon,
            // so an old screen size cannot become a recommendation score.
            const renderedCell = preview.term._core?._renderService?.dimensions?.css?.cell;
            if (![cellWidth, cellHeight, cols, rows].every(value => Number.isFinite(value) && value > 0)) {
                throw new Error('Font geometry unavailable');
            }
            if (!renderedCell || !Number.isFinite(renderedCell.width) || !Number.isFinite(renderedCell.height)
                || Math.abs(screen.width - renderedCell.width * cols) > 1
                || Math.abs(screen.height - renderedCell.height * rows) > 1) {
                throw new Error('Font renderer layout is stale');
            }
            return { cols, rows, cellWidth, cellHeight, capacity: cols * rows, aspect: cellHeight / cellWidth };
        } finally {
            if (preview) preview.dispose();
            host.remove();
        }
    }

    function rank(results, goal) {
        return results.filter(result => result.metrics).slice().sort((left, right) => {
            const a = left.metrics;
            const b = right.metrics;
            if (goal === 'aspect') {
                const difference = Math.abs(a.aspect / ASPECT_TARGET - 1) - Math.abs(b.aspect / ASPECT_TARGET - 1);
                if (Math.abs(difference) > 1e-9) return difference;
            }
            return b.capacity - a.capacity || left.index - right.index;
        });
    }

    function sample() {
        const dots = [[0, 0, 1], [0, 1, 2], [0, 2, 4], [1, 0, 8], [1, 1, 16], [1, 2, 32], [0, 3, 64], [1, 3, 128]];
        const lines = [];
        for (let row = 0; row < 5; row += 1) {
            const shapes = [false, true].map(square => {
                let line = '';
                for (let col = 0; col < 10; col += 1) {
                    let bits = 0;
                    for (const [dx, dy, bit] of dots) {
                        const x = (col * 2 + dx - 9.5) / 9;
                        const y = (row * 4 + dy - 9.5) / 9;
                        const distance = square ? Math.max(Math.abs(x), Math.abs(y)) : Math.hypot(x, y);
                        if (Math.abs(distance - 1) < 0.09) bits |= bit;
                    }
                    line += String.fromCharCode(0x2800 + bits);
                }
                return line;
            });
            lines.push(shapes.join('  '));
        }
        lines.push('MMMMMMMMMMMM  iiiiiiiiiiii', '\u250c\u2500\u2500\u2500\u2510 \u2588\u2580\u2584\u2588 \ue0a0 \ue0b0', '\u2514\u2500\u2500\u2500\u2518 \u4e2d\u6587\u6e2c\u8a66 \ud83d\ude00');
        return lines.join('\r\n');
    }

    root.StandTermFontRecommendations = Object.freeze({
        candidates(current, apple) {
            return [...new Set([current, ...(apple ? APPLE_CANDIDATES : WINDOWS_CANDIDATES)].filter(Boolean))];
        },
        measure, openPreview, rank, sample
    });
})(globalThis);
