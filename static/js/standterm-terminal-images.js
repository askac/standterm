(function (root) {
    'use strict';

    const OPTIONS = {
        // Enable pixel queries below without opting into character-size replies.
        enableSizeReports: false,
        pixelLimit: 4 * 1024 * 1024,
        storageLimit: 32,
        sixelSizeLimit: 8 * 1024 * 1024,
        iipSizeLimit: 8 * 1024 * 1024
    };

    class TerminalImages {
        constructor(term, geometryTerm = term) {
            this.term = term;
            this.geometryTerm = geometryTerm;
            this.enabled = false;
            this.addon = null;
            this.windowOptions = null;
            this.disposed = false;
        }

        setEnabled(enabled) {
            enabled = enabled && typeof root.ImageAddon?.ImageAddon === 'function';
            if (this.disposed || this.enabled === enabled) return;
            this.enabled = enabled;
            // Finish decoding the current output before replacing its handlers.
            this.term.write('', () => {
                if (this.disposed || this.enabled !== enabled) return;
                if (!enabled) {
                    this.clearAddon();
                } else if (!this.addon) {
                    const addon = new ImageAddon.ImageAddon(OPTIONS);
                    this.windowOptions = { ...this.term.options.windowOptions };
                    this.addon = addon;
                    try {
                        this.term.loadAddon(addon);
                        if (this.term === this.geometryTerm) {
                            // tmux already has PTY rows/columns. Separate character-size
                            // replies can race pixel replies through asynchronous input.
                            this.term.options.windowOptions = {
                                ...this.windowOptions,
                                getWinSizePixels: true,
                                getCellSizePixels: true
                            };
                        } else {
                            // The unopened Agent mirror needs the visible terminal's cell
                            // geometry for matching cursor movement and image tile sizes.
                            // This adapter is tied to xterm 6.0.0 / addon-image 0.9.0.
                            Object.defineProperty(addon._renderer, 'dimensions', {
                                get: () => this.geometryTerm._core._renderService.dimensions
                            });
                        }
                    } catch (error) {
                        this.clearAddon();
                        console.warn('Terminal image rendering unavailable.', error);
                    }
                }
            });
        }

        drawViewport(context, buffer, cols, rows, cellWidth, cellHeight) {
            if (!this.addon || this.addon.storageUsage <= 0) return;
            const sourceCell = this.addon._renderer.cellSize;
            if (sourceCell.width <= 0 || sourceCell.height <= 0) return;
            for (let row = 0; row < rows; row++) {
                for (let column = 0; column < cols; column++) {
                    const tile = this.addon.extractTileAtBufferCell(column, buffer.baseY + row);
                    if (tile) context.drawImage(tile, column * cellWidth, row * cellHeight,
                        tile.width * cellWidth / sourceCell.width, tile.height * cellHeight / sourceCell.height);
                }
            }
        }

        clearAddon() {
            this.addon?.dispose();
            this.addon = null;
            if (this.windowOptions) this.term.options.windowOptions = this.windowOptions;
            this.windowOptions = null;
        }

        dispose() {
            this.disposed = true;
            this.clearAddon();
        }
    }

    root.StandTermTerminalImages = TerminalImages;
})(globalThis);
