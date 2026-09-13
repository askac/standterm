const assert = require('node:assert/strict');
const { test } = require('node:test');
global.crypto = require('node:crypto').webcrypto;
const routes = require('../static/js/standterm-ssh-routes.js');

function graph(links, starts = ['A']) {
    return { version: 2, revision: 7, history: [],
        profiles: starts.map((start, i) => ({ id: `entry-${i}`, name: start, startNodeId: start, keyId: null })),
        nodes: Object.entries(links).map(([id, nextNodeId]) => ({ id, nextNodeId,
            endpoint: { host: `${id}.test`, port: '22', username: 'operator' },
            hostKeyAlias: '', authentication: { method: 'password' } })) };
}

test('resolve full tails before the hop limit and distinguish node IDs from IPs', () => {
    const state = graph({ A: 'B', B: 'C', C: 'D', D: 'E', E: 'F', F: 'D' });
    assert.equal(routes.resolve(state, 'A').error, 'cycle');
    state.nodes.at(-1).nextNodeId = null;
    assert.equal(routes.resolve(state, 'A').error, 'depth');
    state.nodes[3].nextNodeId = null;
    state.nodes.forEach(node => { node.endpoint.host = '192.168.167.254'; });
    assert.equal(routes.checkedPath(state, state.profiles[0]).length, 4);
    state.nodes[3].nextNodeId = 'missing';
    assert.equal(routes.resolve(state, 'A').error, 'missing');
});

test('shared tails are valid and scoped edits copy every shared predecessor', () => {
    const state = graph({ A: 'C', X: 'C', C: 'B', B: null }, ['A', 'X']);
    routes.validate(state);
    assert.equal(routes.references(state, 'B').length, 2);
    const untouched = routes.clone(routes.checkedPath(state, state.profiles[1]));
    routes.replaceNode(state, state.profiles[0], 2, {
        ...state.nodes.at(-1), endpoint: { host: 'edited.test', port: '22', username: 'operator' }
    });
    assert.deepEqual(routes.checkedPath(state, state.profiles[1]), untouched);
    assert.equal(routes.checkedPath(state, state.profiles[0]).at(-1).endpoint.host, 'edited.test');
    routes.validate(state);
});

test('cycle repairs display different targets, retain originals and reject stale previews', () => {
    const state = graph({ A: 'B', B: 'C', C: 'B' });
    const entry = state.profiles[0];
    const candidates = routes.repairCandidates(state, entry);
    assert.deepEqual(candidates.map(item => item.path.map(node => node.id)), [['A', 'B'], ['A', 'B', 'C']]);
    const stale = routes.clone(state);
    stale.revision += 1;
    assert.throws(() => routes.applyRepair(stale, stale.profiles[0], candidates[0]), /Reload/);
    const originalNodes = routes.clone(state.nodes);
    routes.applyRepair(state, entry, candidates[0]);
    assert.equal(routes.checkedPath(state, entry).at(-1).endpoint.host, 'B.test');
    assert.deepEqual(state.nodes.slice(0, 3), originalNodes);
    routes.validate(state);
});

test('migration keeps key ownership and route copying does not rebind credentials', () => {
    const old = { profiles: [{ id: 'owner', name: 'B', host: 'B.test', port: '22', username: 'u', keyId: 'key-B' }], history: [] };
    const state = routes.migrate(old);
    assert.equal(state.profiles[0].id, 'owner');
    const node = state.nodes[0];
    assert.equal(node.authentication.keyRef.ownerProfileId, 'owner');
    const copied = routes.copyPath(state, [node]);
    assert.notEqual(copied, node.id);
    assert.deepEqual(state.nodes.at(-1).authentication, node.authentication);
    assert.deepEqual(old.profiles[0], { id: 'owner', name: 'B', host: 'B.test', port: '22', username: 'u', keyId: 'key-B' });
});

test('import remaps all identities, rejects invalid graphs and drops nested secrets and local key refs', () => {
    const state = graph({ A: null });
    state.profiles[0].keyId = 'key-A';
    state.profiles[0].keyTarget = state.nodes[0].endpoint;
    state.nodes[0].password = 'password-sentinel';
    state.nodes[0].authentication = { method: 'browser-key', privateKey: 'private-sentinel', keyRef: {
        ownerProfileId: 'entry-0', keyId: 'key-A', targetKey: routes.endpointKey(state.nodes[0].endpoint)
    } };
    const imported = routes.importState(state, state);
    assert.equal(imported.profiles.length, 2);
    assert.notEqual(imported.profiles[0].id, imported.profiles[1].id);
    assert.notEqual(imported.profiles[0].startNodeId, imported.profiles[1].startNodeId);
    assert.equal(imported.nodes.at(-1).authentication.method, 'browser-key');
    assert.equal(imported.nodes.at(-1).authentication.keyRef, null);
    const exported = JSON.stringify(routes.exportState(state));
    assert.ok(!exported.includes('sentinel'));
    assert.ok(!exported.includes('key-A'));
    assert.throws(() => routes.importState(state, graph({ A: 'A' })), /cycle/);
});
