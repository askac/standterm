(function (root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.StandTermCapabilities = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
    'use strict';

    class CapabilityAddon {
        constructor(sendQuery) {
            this.sendQuery = sendQuery;
            this.queue = [];
            this.current = null;
            this.disposed = false;
        }

        activate(term) {
            this.term = term;
            this.handlers = [
                term.parser.registerCsiHandler({ prefix: '>', final: 'q' }, params => {
                    if (params.length !== 1 || params[0] !== 0) return false;
                    this.query('version');
                    return true;
                }),
                term.parser.registerDcsHandler({ intermediates: '+', final: 'q' }, (names, params) => {
                    if (params.length !== 1 || params[0] !== 0) return false;
                    // The server validates names and constructs the reply; never send raw input.
                    if (names.length <= 1024) this.query('terminfo', names);
                    return true;
                })
            ];
        }

        query(kind, names) {
            const item = this.current;
            if (!item) return;
            if (item.queryCount++ >= 32) return;
            const signature = JSON.stringify([kind, names]);
            const queryIndex = item.queries.get(signature) || 0;
            item.queries.set(signature, queryIndex + 1);
            const context = item.context;
            if (this.disposed || !context || context.replay || !context.capability_epoch) return;
            this.sendQuery({ kind, names, output_seq: context.output_seq,
                capability_epoch: context.capability_epoch, query_index: queryIndex });
        }

        write(data, context, callback) {
            if (this.disposed) return;
            this.queue.push({ data, context, callback, queries: new Map(), queryCount: 0 });
            this.drain();
        }

        drain() {
            if (this.disposed || this.current || !this.queue.length) return;
            const item = this.current = this.queue.shift();
            const epoch = item.context?.capability_epoch;
            const cancel = epoch && this.epoch && epoch !== this.epoch ? '\x18' : '';
            if (epoch) this.epoch = epoch;
            // Preserve source metadata until xterm has finished parsing this write.
            this.term.write(cancel + item.data, () => {
                this.current = null;
                if (!this.disposed && item.callback) item.callback();
                this.drain();
            });
        }

        dispose() {
            this.disposed = true;
            this.queue.length = 0;
            for (const handler of this.handlers || []) handler.dispose();
        }
    }

    return CapabilityAddon;
});
