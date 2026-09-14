(function() {
    'use strict';
    const routes = window.StandTermSshRoutes;

    window.StandTermSshNodeAuth = function({ parent, role, authentication, endpoint, profiles,
        savedKeys, newKeys, keyAllowed, createKey, copyPublicKey, onBusy, onChange }) {
        const label = document.createElement('label');
        label.className = 'ssh-route-auth';
        label.textContent = 'Authentication';
        const method = document.createElement('select');
        method.setAttribute('aria-label', `${role} Authentication`);
        method.add(new Option('Password (entered when connecting)', 'password'));
        method.add(new Option('Browser key', 'browser-key'));
        method.value = authentication.method;
        label.append(method);
        const panel = document.createElement('div');
        panel.className = 'ssh-node-key';
        const keys = document.createElement('select');
        keys.setAttribute('aria-label', `${role} Browser key`);
        const status = document.createElement('p');
        const publicKey = document.createElement('textarea');
        publicKey.className = 'ssh-node-public-key';
        publicKey.setAttribute('aria-label', `${role} Public key`);
        publicKey.readOnly = true;
        publicKey.rows = 3;
        const create = document.createElement('button');
        create.type = 'button';
        create.textContent = 'Create key';
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.textContent = 'Copy public key';
        panel.append(keys, status, publicKey, create, copy);
        parent.append(label, panel);
        let selected = authentication.keyRef || null;
        let generating = false;
        const records = () => [...savedKeys, ...newKeys.values()];
        function reference(record) {
            return { ...(record.kind === 'credential' ? { kind: 'credential' } : { ownerProfileId: record.ownerProfileId }),
                keyId: record.keyId, targetKey: record.targetKey };
        }
        function update() {
            panel.hidden = method.value !== 'browser-key';
            let targetKey;
            try { targetKey = routes.endpointKey(routes.endpoint(endpoint())); } catch (_) { targetKey = null; }
            const candidates = records().filter(record => record.targetKey === targetKey && (record.kind === 'credential'
                || profiles.some(profile => profile.id === record.ownerProfileId && profile.keyId === record.keyId)));
            keys.replaceChildren(new Option('Choose a key...', ''));
            for (const record of candidates) {
                const owner = profiles.find(profile => profile.id === record.ownerProfileId);
                keys.add(new Option(`${record.kind === 'credential' ? 'Browser key' : owner.name} · ${newKeys.has(record.keyId) ? 'Not saved' : record.fingerprint}`, record.keyId));
            }
            const record = selected && candidates.find(item => item.keyId === selected.keyId
                && JSON.stringify(reference(item)) === JSON.stringify(selected));
            if (selected && !record) keys.add(new Option('Key unavailable for this endpoint — choose another key', selected.keyId));
            keys.value = selected?.keyId || '';
            const saved = record && !newKeys.has(record.keyId);
            publicKey.hidden = copy.hidden = !saved;
            publicKey.value = saved ? record.publicKeyOpenSsh : '';
            create.disabled = !targetKey || !keyAllowed || generating;
            keys.disabled = generating;
            status.textContent = generating ? 'Generating browser key…' : !keyAllowed
                ? 'Browser keys require localhost or an authorized HTTPS connection.'
                : record ? saved ? 'Add this public key to the remote account’s authorized_keys before using it.'
                    : 'Save route to keep this key and show its public key.'
                    : selected ? 'Choose a key bound to this host, port and username.' : 'Choose an existing key or create one for this node.';
        }
        method.onchange = () => { update(); onChange(); };
        keys.onchange = () => {
            selected = records().find(record => record.keyId === keys.value);
            selected = selected ? reference(selected) : null;
            update(); onChange();
        };
        create.onclick = async () => {
            if (generating || !keyAllowed) return;
            let target;
            try { target = routes.endpoint(endpoint()); } catch (err) { status.textContent = err.message; return; }
            generating = true;
            onBusy(1);
            update();
            let errorMessage = '';
            try {
                const record = await createKey(target);
                if (!panel.isConnected || routes.endpointKey(routes.endpoint(endpoint())) !== routes.endpointKey(target)) return;
                newKeys.set(record.keyId, record);
                selected = reference(record);
            } catch (err) {
                errorMessage = err.message || 'Browser key creation failed.';
            } finally {
                generating = false;
                onBusy(-1);
                if (panel.isConnected) {
                    update();
                    if (errorMessage) status.textContent = errorMessage;
                    onChange();
                }
            }
        };
        copy.onclick = () => {
            const record = savedKeys.find(item => item.keyId === selected?.keyId && item.targetKey === routes.endpointKey(routes.endpoint(endpoint())));
            if (record) copyPublicKey(record.publicKeyOpenSsh);
        };
        update();
        return { update, read: () => method.value === 'password' ? { method: 'password' }
            : { method: 'browser-key', keyRef: selected } };
    };
})();
