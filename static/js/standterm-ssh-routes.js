(function(root, factory) {
    const api = factory();
    if (typeof module === 'object' && module.exports) module.exports = api;
    else root.StandTermSshRoutes = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function() {
    'use strict';

    const MAX_JUMPS = 3;
    const PREFIX = 'routes-v2:';
    const META = `${PREFIX}catalog`;
    const ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/;
    const clone = value => JSON.parse(JSON.stringify(value));
    const id = () => `node-${typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`}`;

    function endpoint(value) {
        const port = Number(value.port);
        const host = typeof value.host === 'string' ? value.host.trim() : '';
        const username = typeof value.username === 'string' ? value.username.trim() : '';
        if (!host || host.length > 255 || /[\s,|*?!#@\x00-\x1f\x7f]/.test(host)
                || !username || username.length > 128 || /[\x00-\x1f\x7f]/.test(username)
                || !Number.isInteger(port) || port < 1 || port > 65535) {
            throw new Error('Each SSH node needs a valid host, port and username.');
        }
        return { host, port: String(port), username };
    }

    function endpointKey(value) {
        return [value.host.toLowerCase(), String(value.port), value.username].join('\u0000');
    }

    function publicNode(value) {
        if (!value || !ID_PATTERN.test(value.id || '')
                || (value.nextNodeId !== null && !ID_PATTERN.test(value.nextNodeId || ''))) {
            throw new Error('Invalid SSH node reference.');
        }
        const alias = value.hostKeyAlias || '';
        if (typeof alias !== 'string' || alias.length > 255 || /[\s,|*?!#@\x00-\x1f\x7f]/.test(alias)) {
            throw new Error('Invalid SSH host key alias.');
        }
        const auth = value.authentication || { method: 'password' };
        if (!['password', 'browser-key'].includes(auth.method)) throw new Error('Invalid SSH authentication method.');
        const authentication = { method: auth.method };
        if (auth.method === 'browser-key') {
            const ref = auth.keyRef;
            if (ref && ((ref.kind === 'credential' ? ref.ownerProfileId !== undefined
                    : ref.kind !== undefined || !ID_PATTERN.test(ref.ownerProfileId || '')) || !ID_PATTERN.test(ref.keyId || '')
                    || ref.targetKey !== endpointKey(endpoint(value.endpoint)))) {
                throw new Error('Choose a browser key bound to this node endpoint, or choose password authentication.');
            }
            authentication.keyRef = ref ? { ...(ref.kind === 'credential' ? { kind: 'credential' }
                : { ownerProfileId: ref.ownerProfileId }), keyId: ref.keyId, targetKey: ref.targetKey } : null;
        }
        return { id: value.id, endpoint: endpoint(value.endpoint), authentication,
            hostKeyAlias: alias, nextNodeId: value.nextNodeId };
    }

    function resolve(state, startNodeId) {
        const nodes = new Map(state.nodes.map(node => [node.id, node]));
        const seen = new Map();
        const path = [];
        let next = startNodeId;
        while (next !== null) {
            if (seen.has(next)) return { path, error: 'cycle', cycleIndex: seen.get(next), repeatedId: next };
            const node = nodes.get(next);
            if (!node) return { path, error: 'missing', missingId: next };
            seen.set(next, path.length);
            path.push(node);
            next = node.nextNodeId;
        }
        return { path, error: path.length > MAX_JUMPS + 1 ? 'depth' : path.length ? null : 'empty' };
    }

    function checkedPath(state, entry) {
        const result = resolve(state, entry.startNodeId);
        if (result.error) {
            const error = new Error(result.error === 'depth'
                ? `Use at most ${MAX_JUMPS} jump hosts plus the target.`
                : `SSH route is invalid: ${result.error}.`);
            error.code = result.error;
            error.route = result;
            throw error;
        }
        return result.path;
    }

    function routeKey(path) {
        return JSON.stringify(path.map(node => [node.endpoint.host.toLowerCase(), String(node.endpoint.port),
            node.endpoint.username, node.hostKeyAlias || '']));
    }

    function references(state, nodeId) {
        return [...state.profiles, ...state.history].filter(entry =>
            resolve(state, entry.startNodeId).path.some(node => node.id === nodeId));
    }

    function copyPath(state, path) {
        const copies = path.map(node => ({ ...clone(node), id: id() }));
        copies.forEach((node, index) => { node.nextNodeId = copies[index + 1]?.id || null; });
        state.nodes.push(...copies);
        return copies[0]?.id || null;
    }

    function replaceNode(state, entry, index, replacement, scope = 'entry') {
        const path = checkedPath(state, entry);
        if (!path[index]) throw new Error('SSH node no longer exists.');
        const updated = publicNode({ ...replacement, id: path[index].id });
        if (scope === 'all') {
            state.nodes[state.nodes.findIndex(node => node.id === updated.id)] = updated;
        } else {
            const prefix = path.slice(0, index + 1).map(node => ({ ...clone(node), id: id() }));
            prefix[index] = { ...updated, id: prefix[index].id };
            prefix.slice(0, -1).forEach((node, i) => { node.nextNodeId = prefix[i + 1].id; });
            state.nodes.push(...prefix);
            entry.startNodeId = prefix[0].id;
        }
    }

    function repairCandidates(state, entry) {
        const result = resolve(state, entry.startNodeId);
        if (result.error !== 'cycle') return [];
        return [
            { rule: 'erase-loop', path: result.path.slice(0, result.cycleIndex + 1) },
            { rule: 'before-back-edge', path: result.path }
        ].filter(candidate => candidate.path.length <= MAX_JUMPS + 1)
            .map(candidate => ({ ...candidate, revision: state.revision, entryId: entry.id }));
    }

    function applyRepair(state, entry, preview) {
        if (state.revision !== preview.revision || entry.id !== preview.entryId) throw new Error('Reload the changed SSH route before repairing it.');
        const candidate = repairCandidates(state, entry).find(item => item.rule === preview.rule);
        if (!candidate || JSON.stringify(candidate) !== JSON.stringify(preview)) throw new Error('SSH repair preview is stale.');
        entry.startNodeId = copyPath(state, candidate.path);
        checkedPath(state, entry);
    }

    function validate(state) {
        const seen = new Set();
        state.nodes.forEach(node => {
            publicNode(node);
            if (seen.has(node.id)) throw new Error('Duplicate SSH node id.');
            seen.add(node.id);
        });
        // Unreachable nodes remain independently stored. Entry resolution always checks the full tail.
        state.nodes.forEach(node => {
            if (node.nextNodeId !== null && !seen.has(node.nextNodeId)) throw new Error('SSH node references a missing node.');
        });
        const entries = new Set();
        [...state.profiles, ...state.history].forEach(entry => {
            if (!ID_PATTERN.test(entry.id || '') || entries.has(entry.id)) throw new Error('Invalid or duplicate SSH entry id.');
            entries.add(entry.id);
            checkedPath(state, entry).forEach(node => {
                const ref = node.authentication.keyRef;
                if (!ref) return;
                if (ref.kind === 'credential') return;
                const owner = state.profiles.find(profile => profile.id === ref.ownerProfileId);
                if (!owner || owner.keyId !== ref.keyId || endpointKey(owner.keyTarget || owner) !== ref.targetKey) {
                    throw new Error('An SSH route references an unavailable browser key. Edit its authentication before saving.');
                }
            });
        });
        return state;
    }

    function project(state) {
        [...state.profiles, ...state.history].forEach(entry => {
            const result = resolve(state, entry.startNodeId);
            const target = result.path.at(-1);
            if (target) Object.assign(entry, target.endpoint);
        });
        return state;
    }

    function migrate(legacy) {
        const state = { version: 2, revision: 0, profiles: [], history: [], nodes: [] };
        for (const kind of ['profiles', 'history']) {
            for (const old of legacy[kind] || []) {
                const target = endpoint(old);
                const node = { id: id(), endpoint: target, hostKeyAlias: '', nextNodeId: null,
                    authentication: old.keyId ? { method: 'browser-key', keyRef: {
                        ownerProfileId: old.id, keyId: old.keyId, targetKey: endpointKey(target)
                    } } : { method: 'password' } };
                state.nodes.push(node);
                state[kind].push({ ...old, startNodeId: node.id, keyTarget: old.keyId ? target : null });
            }
        }
        return state;
    }

    function sanitize(value) {
        const entry = item => ({
            id: item.id, name: String(item.name || '').slice(0, 64), startNodeId: item.startNodeId,
            sortOrder: Number(item.sortOrder) || 0, lastUsedAt: String(item.lastUsedAt || ''),
            keyId: typeof item.keyId === 'string' ? item.keyId : null,
            keyTarget: item.keyTarget ? endpoint(item.keyTarget) : null
        });
        return project({ version: 2, revision: value.revision, nodes: value.nodes.map(publicNode),
            profiles: value.profiles.map(entry), history: value.history.map(entry) });
    }

    function syncLegacyEdits(value) {
        const state = clone(value);
        for (const entry of [...state.profiles, ...state.history]) {
            if (!entry.startNodeId) {
                const node = { id: id(), endpoint: endpoint(entry), hostKeyAlias: '',
                    nextNodeId: null, authentication: { method: 'password' } };
                if (entry.keyId) {
                    entry.keyTarget = node.endpoint;
                    node.authentication = { method: 'browser-key', keyRef: {
                        ownerProfileId: entry.id, keyId: entry.keyId, targetKey: endpointKey(node.endpoint)
                    } };
                }
                state.nodes.push(node);
                entry.startNodeId = node.id;
            } else {
                const path = checkedPath(state, entry);
                const target = path.at(-1);
                if (endpointKey(entry) !== endpointKey(target.endpoint)) {
                    replaceNode(state, entry, path.length - 1, { ...target, endpoint: endpoint(entry) });
                }
            }
        }
        return sanitize(state);
    }

    function exportState(state) {
        const result = sanitize(state);
        const reachable = new Set([...result.profiles, ...result.history].flatMap(entry =>
            resolve(result, entry.startNodeId).path.map(node => node.id)));
        result.nodes = result.nodes.filter(node => reachable.has(node.id));
        result.profiles.forEach(entry => { delete entry.keyId; delete entry.keyTarget; });
        [...result.profiles, ...result.history].forEach(entry => {
            delete entry.host;
            delete entry.port;
            delete entry.username;
        });
        result.nodes.forEach(node => { if (node.authentication.method === 'browser-key') node.authentication.keyRef = null; });
        return result;
    }

    function importState(current, incoming) {
        const source = exportState(incoming);
        validate(source);
        const mapping = new Map(source.nodes.map(node => [node.id, id()]));
        const result = clone(current);
        source.nodes.forEach(node => result.nodes.push({ ...node, id: mapping.get(node.id),
            nextNodeId: node.nextNodeId === null ? null : mapping.get(node.nextNodeId) }));
        for (const kind of ['profiles', 'history']) {
            source[kind].forEach(entry => result[kind].push({ ...entry, id: id(), startNodeId: mapping.get(entry.startNodeId) }));
        }
        return validate(project(result));
    }

    async function load(openDb, normalizeLegacy, includeKeys = false) {
        const db = await openDb();
        return new Promise((resolvePromise, reject) => {
            const tx = db.transaction(includeKeys ? ['state', 'keys'] : ['state'], 'readonly');
            const store = tx.objectStore('state');
            const keys = store.getAllKeys();
            const values = store.getAll();
            const keyRecords = includeKeys ? tx.objectStore('keys').getAll() : null;
            tx.oncomplete = () => {
                db.close();
                try {
                    const records = new Map(keys.result.map((key, i) => [key, values.result[i]]));
                    const meta = records.get(META);
                    const finish = state => resolvePromise(includeKeys ? { state, keys: keyRecords.result } : state);
                    if (!meta) { finish(migrate(normalizeLegacy(records.get('current')))); return; }
                    const result = { version: 2, revision: meta.revision,
                        profiles: meta.profiles.map(key => records.get(`${PREFIX}entry:${key}`)),
                        history: meta.history.map(key => records.get(`${PREFIX}entry:${key}`)),
                        nodes: meta.nodes.map(key => records.get(`${PREFIX}node:${key}`)) };
                    finish(sanitize(result));
                } catch (err) { reject(err); }
            };
            tx.onabort = tx.onerror = () => { db.close(); reject(tx.error || new Error('SSH routes could not be loaded.')); };
        });
    }

    async function save(openDb, value, keyChanges = []) {
        const state = validate(sanitize(value));
        const db = await openDb();
        return new Promise((resolvePromise, reject) => {
            const tx = db.transaction(['state', 'keys'], 'readwrite');
            const store = tx.objectStore('state');
            let failure;
            const request = store.get(META);
            request.onsuccess = () => {
                const previous = request.result;
                if ((previous?.revision || 0) !== state.revision) {
                    failure = new Error('SSH settings changed in another window. Reload before saving.');
                    failure.code = 'stale';
                    tx.abort();
                    return;
                }
                const keyStore = tx.objectStore('keys');
                const keyRequest = keyStore.getAll();
                keyRequest.onsuccess = () => {
                    const records = new Map(keyRequest.result.map(record => [record.keyId, record]));
                    keyChanges.forEach(change => {
                        if (change.type === 'put') records.set(change.record.keyId, change.record);
                        if (change.type === 'delete') records.delete(change.keyId);
                    });
                    for (const node of state.nodes) {
                        const ref = node.authentication.keyRef;
                        if (ref?.kind !== 'credential') continue;
                        const record = records.get(ref.keyId);
                        if (!record || record.kind !== 'credential' || record.ownerProfileId !== null
                                || record.targetKey !== ref.targetKey || record.privateKey?.extractable !== false) {
                            failure = new Error('An SSH node references an unavailable browser credential.');
                            tx.abort();
                            return;
                        }
                    }
                    // Publish the graph and key changes in this transaction.
                    const entryIds = new Set([...state.profiles, ...state.history].map(entry => entry.id));
                    for (const key of [...(previous?.profiles || []), ...(previous?.history || [])]) {
                        if (!entryIds.has(key)) store.delete(`${PREFIX}entry:${key}`);
                    }
                    state.revision += 1;
                    for (const item of [...state.profiles, ...state.history]) {
                        const record = { ...item };
                        delete record.host;
                        delete record.port;
                        delete record.username;
                        store.put(record, `${PREFIX}entry:${item.id}`);
                    }
                    for (const item of state.nodes) store.put(item, `${PREFIX}node:${item.id}`);
                    store.put({ revision: state.revision, profiles: state.profiles.map(item => item.id),
                        history: state.history.map(item => item.id), nodes: state.nodes.map(item => item.id) }, META);
                    keyChanges.forEach(change => {
                        if (change.type === 'put') keyStore.put(change.record);
                        if (change.type === 'delete') keyStore.delete(change.keyId);
                    });
                };
            };
            tx.oncomplete = () => { db.close(); resolvePromise(state); };
            tx.onabort = tx.onerror = () => { db.close(); reject(failure || tx.error || new Error('SSH route save was aborted.')); };
        });
    }

    return { MAX_JUMPS, id, clone, endpoint, endpointKey, publicNode, resolve, checkedPath, routeKey,
        references, copyPath, replaceNode, repairCandidates, applyRepair, validate, project,
        migrate, sanitize, syncLegacyEdits, exportState, importState, load, save };
});
