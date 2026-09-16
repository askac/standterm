'use strict';

const assert = require('node:assert/strict');
const { Terminal } = require('../static/js/xterm.js');
const CapabilityAddon = require('../static/js/standterm-capabilities.js');

async function run() {
    const queries = [];
    const input = [];
    const term = new Terminal({ allowProposedApi: true });
    const addon = new CapabilityAddon(query => queries.push(query));
    term.loadAddon(addon);
    term.onData(data => input.push(data));
    const context = (seq, replay = false) => ({ output_seq: seq, replay, capability_epoch: 'bridge-1' });
    const write = (data, ctx) => new Promise(resolve => addon.write(data, ctx, resolve));
    try {
        await Promise.all([
            write('\x1b[>q\x1bP+q524742;436f\x1b\\', context(1)),
            write('\x1b[>q', context(2, true)),
            write('\x1bP+q52', context(3, true)),
            write('4742\x1b\\\x1b[>0q', context(4))
        ]);
        assert.deepEqual(queries.map(q => [q.kind, q.output_seq, q.query_index]),
            [['version', 1, 0], ['terminfo', 1, 0], ['terminfo', 4, 0], ['version', 4, 0]]);
        assert.equal(queries[1].names, '524742;436f');
        assert.deepEqual(input, [], 'automatic replies must not enter human input');
        await write('\x1bP+q' + '41'.repeat(1025) + '\x1b\\', context(5));
        assert.equal(queries.length, 4);
        await write('\x1b[38;2;12;34;56mX\x1b[0m', context(6));
        const cell = term.buffer.active.getLine(0).getCell(0);
        assert.ok(cell.isFgRGB());
        assert.equal(cell.getFgColor(), 0x0c2238);
        await write('\x1b[?69$p', context(7));
        assert.deepEqual(input, ['\x1b[?69;0$y'], 'unsupported margins must stay unsupported');
        await write('\x1bP+q52', context(8));
        await write('4742\x1b\\', { ...context(1), capability_epoch: 'bridge-2' });
        assert.equal(queries.length, 4, 'a new bridge must not finish an old query');
        // A late viewer without the DCS prefix must still identify the following version query identically.
        const lateQueries = [];
        const late = new Terminal({ allowProposedApi: true });
        const lateAddon = new CapabilityAddon(query => lateQueries.push(query));
        late.loadAddon(lateAddon);
        await new Promise(resolve => lateAddon.write('4742\x1b\\\x1b[>0q', context(4), resolve));
        assert.deepEqual(lateQueries[0], queries[3]);
        late.dispose();
        console.log('Shipped xterm: live, replay, fragmented queries, input isolation, RGB: ok');
    } finally {
        term.dispose();
    }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
