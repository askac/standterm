// StandTerm IME positioning PoC, not a permanent xterm.js fix.
// Geometry follows xterm.js 6.0.0 CompositionHelper (MIT); see
// /static/licenses/xtermjs-MIT-LICENSE.txt. Text and input handling stay upstream.
(() => {
    'use strict';

    window.StandTermImeAnchorPoc = class {
        activate(term) {
            const core = term._core;
            const helper = core?._compositionHelper;
            const view = core?._compositionView;
            const area = term.textarea;
            const element = term.element;
            if (!helper || typeof helper.updateCompositionElements !== 'function'
                || !view || !area || !element || !core._renderService
                || typeof term.registerMarker !== 'function' || !term.options.allowProposedApi
                || typeof term.onScroll !== 'function' || typeof term.onResize !== 'function'
                || typeof term.buffer?.onBufferChange !== 'function') return;

            const original = helper.updateCompositionElements;
            let anchor = null;
            let timer = null;
            let disposed = false;
            const clearAnchor = () => {
                anchor?.marker.dispose();
                anchor = null;
                if (timer !== null) window.clearTimeout(timer);
                timer = null;
            };
            const start = () => {
                clearAnchor();
                const buffer = term.buffer.active;
                const row = buffer.baseY + buffer.cursorY - buffer.viewportY;
                if (row < 0 || row >= term.rows) return;
                try {
                    const marker = term.registerMarker(0);
                    if (marker) anchor = { buffer, marker, column: Math.min(buffer.cursorX, term.cols - 1) };
                } catch {
                    // A changed proposed API must not break native IME input.
                    clearAnchor();
                }
            };
            const update = (dontRecurse) => {
                const buffer = term.buffer.active;
                const cell = core._renderService.dimensions?.css?.cell;
                const row = anchor ? anchor.marker.line - buffer.viewportY : -1;
                if (!helper.isComposing || !anchor || anchor.buffer !== buffer || anchor.marker.isDisposed
                    || row < 0 || row >= term.rows || !(cell?.width > 0) || !(cell?.height > 0)) {
                    clearAnchor();
                    return original.call(helper, dontRecurse);
                }

                // Follow the input line, not the application's temporary drawing cursor.
                const left = Math.min(anchor.column, term.cols - 1) * cell.width;
                const top = row * cell.height;
                view.style.left = left + 'px';
                view.style.top = top + 'px';
                view.style.height = cell.height + 'px';
                view.style.lineHeight = cell.height + 'px';
                view.style.fontFamily = term.options.fontFamily;
                view.style.fontSize = term.options.fontSize + 'px';
                const bounds = view.getBoundingClientRect();
                area.style.left = left + 'px';
                area.style.top = top + 'px';
                area.style.width = Math.max(bounds.width, 1) + 'px';
                area.style.height = Math.max(bounds.height, 1) + 'px';
                area.style.lineHeight = bounds.height + 'px';
                if (!dontRecurse) {
                    if (timer !== null) window.clearTimeout(timer);
                    timer = window.setTimeout(() => {
                        timer = null;
                        if (!disposed) update(true);
                    }, 0);
                }
            };
            const end = () => {
                clearAnchor();
                // xterm's own compositionend handler runs first and owns sending text.
                if (!helper.isComposing) core._syncTextArea?.();
            };
            const release = () => {
                clearAnchor();
                original.call(helper);
            };
            helper.updateCompositionElements = update;
            area.addEventListener('compositionstart', start, true);
            area.addEventListener('compositionend', end);
            area.addEventListener('blur', clearAnchor);
            const listeners = [
                term.onScroll(() => update()),
                // Column reflow can invalidate an anchor column. Fall back for this
                // composition instead of guessing; the next composition gets a new anchor.
                term.onResize(release),
                term.buffer.onBufferChange(release)
            ];
            element.dataset.imeAnchorPoc = 'enabled';
            this._dispose = () => {
                disposed = true;
                clearAnchor();
                for (const listener of listeners) listener.dispose();
                area.removeEventListener('compositionstart', start, true);
                area.removeEventListener('compositionend', end);
                area.removeEventListener('blur', clearAnchor);
                if (helper.updateCompositionElements === update) helper.updateCompositionElements = original;
                delete element.dataset.imeAnchorPoc;
            };
        }

        dispose() {
            this._dispose?.();
            this._dispose = null;
        }
    };
})();
