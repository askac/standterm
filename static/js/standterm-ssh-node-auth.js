(function() {
    'use strict';
    const routes = window.StandTermSshRoutes;

    window.StandTermSshNodeAuth = function({ parent, role, authentication, endpoint, profiles,
        savedKeys, newKeys, keyAllowed, createKey, copyPublicKey, onBusy, onChange,
        useKeyInput, labelElement, resetOnEndpointChange = false }) {
        const panel = document.createElement('div');
        panel.className = 'ssh-node-key';
        const row = document.createElement('div');
        row.className = 'ssh-key-row';
        const label = labelElement || document.createElement('label');
        label.className = 'ssh-key-toggle';
        label.hidden = false;
        const useKey = useKeyInput || document.createElement('input');
        useKey.type = 'checkbox';
        useKey.setAttribute('aria-label', `${role} Use key`);
        useKey.checked = authentication.method === 'browser-key';
        label.replaceChildren(useKey, document.createTextNode('Use key'));
        const publicKey = document.createElement('input');
        publicKey.type = 'text';
        publicKey.className = 'ssh-node-public-key';
        publicKey.setAttribute('aria-label', `${role} Public key`);
        publicKey.readOnly = true;
        publicKey.placeholder = 'No key for this endpoint';
        const copy = document.createElement('button');
        copy.type = 'button';
        copy.className = 'ssh-key-copy';
        copy.innerHTML = '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></svg>';
        copy.title = 'Copy public key';
        copy.setAttribute('aria-label', 'Copy public key');
        row.append(label, publicKey, copy);
        const keys = document.createElement('select');
        keys.setAttribute('aria-label', `${role} Browser key`);
        const status = document.createElement('p');
        status.className = 'ssh-key-status';
        panel.append(row, keys, status);
        parent.append(panel);
        let selected = authentication.keyRef || null;
        let generating = false;
        let currentRecord = null;
        let previousTarget = null;
        const records = () => [...savedKeys, ...newKeys.values()];
        function reference(record) {
            return { ...(record.kind === 'credential' ? { kind: 'credential' } : { ownerProfileId: record.ownerProfileId }),
                keyId: record.keyId, targetKey: record.targetKey };
        }
        function targetKey() {
            try { return routes.endpointKey(routes.endpoint(endpoint())); } catch (_) { return null; }
        }
        function candidates() {
            return records().filter(record => record.targetKey === targetKey() && (record.kind === 'credential'
                || profiles.some(profile => profile.id === record.ownerProfileId && profile.keyId === record.keyId)));
        }
        function update() {
            const target = targetKey();
            if (resetOnEndpointChange && previousTarget !== target) {
                selected = null;
                useKey.checked = false;
            }
            previousTarget = target;
            const available = candidates();
            if (!selected && available.length === 1) selected = reference(available[0]);
            currentRecord = selected && available.find(record => record.keyId === selected.keyId
                && JSON.stringify(reference(record)) === JSON.stringify(selected));
            keys.replaceChildren(new Option('Choose a saved key...', ''));
            for (const record of available) keys.add(new Option(record.fingerprint, record.keyId));
            if (selected && !currentRecord) keys.add(new Option('Saved key unavailable for this endpoint', selected.keyId));
            keys.value = selected?.keyId || '';
            keys.hidden = available.length < 2 && (!selected || !!currentRecord);
            keys.disabled = generating || !keyAllowed;
            useKey.disabled = generating || !keyAllowed || !target;
            publicKey.value = currentRecord?.publicKeyOpenSsh || '';
            publicKey.title = currentRecord?.fingerprint || '';
            publicKey.classList.toggle('inactive', !useKey.checked);
            copy.disabled = !currentRecord;
            status.textContent = generating ? 'Creating key…' : !keyAllowed
                ? 'Key authentication requires localhost or an authorized HTTPS connection.'
                : currentRecord ? newKeys.has(currentRecord.keyId)
                    ? 'Temporary key. Save the session or route to keep it.'
                    : 'Saved key. Copy it to the remote account’s authorized_keys if needed.'
                : selected ? 'The selected key is unavailable. Choose a saved key or turn Use key off and on to create one.'
                : available.length > 1 ? 'Choose which saved key to use.'
                : 'Select Use key to create a key for this endpoint.';
        }
        useKey.onchange = async () => {
            if (!useKey.checked) { update(); onChange(); return; }
            const available = candidates();
            if (!currentRecord) selected = available.length === 1 ? reference(available[0]) : null;
            update();
            onChange();
            if (currentRecord || available.length > 1 || generating || !keyAllowed) return;
            let target;
            try { target = routes.endpoint(endpoint()); } catch (err) { status.textContent = err.message; return; }
            generating = true;
            onBusy(1);
            update();
            let errorMessage = '';
            try {
                const isCurrent = () => panel.isConnected && targetKey() === routes.endpointKey(target);
                const record = await createKey(target);
                newKeys.set(record.keyId, record);
                if (isCurrent()) selected = reference(record);
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
        keys.onchange = () => {
            const record = candidates().find(item => item.keyId === keys.value);
            selected = record ? reference(record) : null;
            update(); onChange();
        };
        copy.onclick = () => {
            if (currentRecord && currentRecord.targetKey === targetKey()) {
                copyPublicKey(currentRecord.publicKeyOpenSsh);
            }
        };
        update();
        return { update, record: () => currentRecord, read: () => useKey.checked
            ? { method: 'browser-key', keyRef: selected } : { method: 'password' },
            configure: options => {
                if (options.savedKeys) savedKeys = options.savedKeys;
                if (options.profiles) profiles = options.profiles;
                if (options.keyAllowed !== undefined) keyAllowed = options.keyAllowed;
                update();
                if (options.authentication) {
                    selected = options.authentication.keyRef || null;
                    useKey.checked = options.authentication.method === 'browser-key';
                    update();
                }
            }
        };
    };
})();
