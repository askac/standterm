(function() {
    'use strict';
    const routes = window.StandTermSshRoutes;

    routes.edit = function({ state, entryId, target, save, onSaved }) {
        const original = routes.clone(state);
        let draft = routes.clone(state);
        let entry = [...draft.profiles, ...draft.history].find(item => item.id === entryId);
        if (!entry) {
            const node = { id: routes.id(), endpoint: routes.endpoint(target), nextNodeId: null,
                hostKeyAlias: '', authentication: { method: 'password' } };
            entry = { id: routes.id(), name: `${target.username}@${target.host}`, startNodeId: node.id,
                sortOrder: draft.profiles.length, keyId: null, keyTarget: null };
            draft.nodes.push(node);
            draft.profiles.push(entry);
        }
        const dialog = document.createElement('dialog');
        dialog.id = 'ssh-route-editor';
        const title = document.createElement('h3');
        title.textContent = 'SSH route';
        const name = document.createElement('input');
        name.value = entry.name || '';
        name.placeholder = 'Entry name';
        name.maxLength = 64;
        name.setAttribute('aria-label', 'Entry name');
        const scope = document.createElement('select');
        scope.setAttribute('aria-label', 'Edit scope');
        for (const [value, label] of [['entry', 'Only this Entry'], ['all', 'Apply node edits to all references']]) {
            scope.add(new Option(label, value));
        }
        const rows = document.createElement('div');
        const status = document.createElement('p');
        status.setAttribute('role', 'status');
        const actions = document.createElement('div');
        actions.className = 'ssh-route-buttons ssh-route-actions';
        const button = (label, callback, parent = actions) => {
            const element = document.createElement('button');
            element.type = 'button';
            element.textContent = label;
            element.onclick = () => {
                try { Promise.resolve(callback()).catch(fail); } catch (err) { fail(err); }
            };
            parent.append(element);
            return element;
        };
        const fail = err => { status.textContent = err.message || 'SSH route could not be saved.'; };
        let controls = [];
        const labelPath = path => `Core host → ${path.map(node => `${node.endpoint.username}@${node.endpoint.host}:${node.endpoint.port}`).join(' → ')}`;

        function flush(skipIndex = -1) {
            const previousPath = routes.checkedPath(draft, entry);
            const updated = controls.map((control, index) => index === skipIndex ? previousPath[index] : control.read());
            updated.forEach((node, index) => {
                if (JSON.stringify(node) !== JSON.stringify(previousPath[index])) {
                    routes.replaceNode(draft, entry, index, node, scope.value);
                }
            });
            entry.name = name.value.trim();
            if (!entry.name) throw new Error('Entry name is required.');
            routes.project(draft);
        }

        function replacePath(path) {
            entry.startNodeId = routes.copyPath(draft, path);
            routes.project(draft);
            render();
        }

        function offerRepair() {
            const result = routes.resolve(draft, entry.startNodeId);
            if (result.error !== 'cycle') return false;
            status.replaceChildren(document.createTextNode(
                `${labelPath(result.path)} → ${result.path[result.cycleIndex].endpoint.host}: cycle rejected. Choose an explicit target or cancel.`));
            for (const candidate of routes.repairCandidates(draft, entry)) {
                const target = candidate.path.at(-1).endpoint;
                button(`${candidate.rule === 'erase-loop' ? 'Remove loop' : 'Stop before back edge'}: ${labelPath(candidate.path)}; target ${target.username}@${target.host}:${target.port}`, () => {
                    // Keep other Entries and shared nodes exactly as they were before this edit.
                    const repaired = routes.clone(original);
                    let selected = [...repaired.profiles, ...repaired.history].find(item => item.id === entry.id);
                    if (!selected) { selected = { ...entry }; repaired.profiles.push(selected); }
                    selected.name = name.value.trim();
                    selected.startNodeId = routes.copyPath(repaired, candidate.path);
                    draft = routes.project(repaired);
                    entry = selected;
                    routes.validate(draft);
                    status.textContent = 'Repair selected. Review the target, then Save route.';
                    render();
                }, status);
            }
            return true;
        }

        function render() {
            controls = [];
            rows.replaceChildren();
            const result = routes.resolve(draft, entry.startNodeId);
            const pathText = document.createElement('p');
            pathText.textContent = labelPath(result.path);
            rows.append(pathText);
            result.path.forEach((node, index) => {
                const fieldset = document.createElement('fieldset');
                const legend = document.createElement('legend');
                legend.textContent = index === result.path.length - 1 ? 'Target' : `Jump host ${index + 1}`;
                fieldset.append(legend);
                const fields = document.createElement('div');
                fields.className = 'ssh-route-fields';
                fieldset.append(fields);
                const input = (label, value) => {
                    const wrapper = document.createElement('label');
                    wrapper.textContent = label;
                    const field = document.createElement('input');
                    field.value = value;
                    field.setAttribute('aria-label', `${legend.textContent} ${label}`);
                    wrapper.append(field);
                    fields.append(wrapper);
                    return field;
                };
                const host = input('Host', node.endpoint.host);
                const port = input('Port', node.endpoint.port);
                const username = input('Username', node.endpoint.username);
                const alias = input('Host key alias (optional)', node.hostKeyAlias);
                const auth = document.createElement('select');
                auth.setAttribute('aria-label', `${legend.textContent} Authentication`);
                auth.add(new Option('Password (entered when connecting)', 'password'));
                const keyRefs = new Map();
                draft.profiles.filter(owner => owner.keyId).forEach(owner => {
                    keyRefs.set(owner.id, { ownerProfileId: owner.id, keyId: owner.keyId,
                        targetKey: routes.endpointKey(owner.keyTarget || owner) });
                    auth.add(new Option(`Browser key from: ${owner.name}`, owner.id));
                });
                const keyRef = node.authentication.keyRef;
                if (node.authentication.method === 'browser-key') {
                    if (keyRef && JSON.stringify(keyRefs.get(keyRef.ownerProfileId)) === JSON.stringify(keyRef)) auth.value = keyRef.ownerProfileId;
                    else { auth.add(new Option('Browser key unavailable — choose authentication', 'unresolved')); auth.value = 'unresolved'; }
                }
                const authLabel = document.createElement('label');
                authLabel.textContent = 'Authentication';
                authLabel.append(auth);
                fields.append(authLabel);
                const affected = document.createElement('p');
                affected.textContent = `Referenced by: ${routes.references(draft, node.id).map(item => item.name || item.host).join(', ')}`;
                fieldset.append(affected);
                controls.push({ read: () => routes.publicNode({ ...node,
                    endpoint: { host: host.value, port: port.value, username: username.value },
                    hostKeyAlias: alias.value.trim(), authentication: auth.value === 'password'
                        ? { method: 'password' } : { method: 'browser-key', keyRef: keyRefs.get(auth.value) || null }
                }) });
                const otherEntries = document.createElement('select');
                otherEntries.setAttribute('aria-label', `${legend.textContent} Next route`);
                otherEntries.add(new Option('Choose the next route...', ''));
                draft.profiles.forEach(item => otherEntries.add(new Option(item.name, item.id)));
                fieldset.append(otherEntries);
                const nodeButtons = document.createElement('div');
                nodeButtons.className = 'ssh-route-buttons';
                fieldset.append(nodeButtons);
                for (const copy of [false, true]) {
                    button(copy ? 'Copy route after this node' : 'Reference route after this node', () => {
                        const selected = draft.profiles.find(item => item.id === otherEntries.value);
                        if (!selected) throw new Error('Choose a route to append.');
                        flush();
                        const previous = routes.clone(draft);
                        try {
                            const nextId = copy ? routes.copyPath(draft, routes.checkedPath(draft, selected)) : selected.startNodeId;
                            const current = routes.checkedPath(draft, entry);
                            routes.replaceNode(draft, entry, index, { ...current[index], nextNodeId: nextId }, scope.value);
                            if (!offerRepair()) { routes.validate(draft); routes.project(draft); render(); }
                        } catch (err) {
                            draft = previous;
                            entry = [...draft.profiles, ...draft.history].find(item => item.id === entry.id);
                            render();
                            throw err;
                        }
                    }, nodeButtons);
                }
                if (index > 0) button('Move earlier', () => {
                    flush();
                    const path = routes.checkedPath(draft, entry);
                    [path[index - 1], path[index]] = [path[index], path[index - 1]];
                    replacePath(path);
                }, nodeButtons);
                if (result.path.length > 1) button('Remove from this Entry', () => {
                    flush(index);
                    replacePath(routes.checkedPath(draft, entry).filter((_, i) => i !== index));
                }, nodeButtons);
                rows.append(fieldset);
            });
        }

        button('Add jump host first', () => {
            flush();
            const path = routes.checkedPath(draft, entry);
            if (path.length >= routes.MAX_JUMPS + 1) throw new Error(`Use at most ${routes.MAX_JUMPS} jump hosts.`);
            const node = { id: routes.id(), endpoint: { host: '', port: '22', username: target.username },
                hostKeyAlias: '', nextNodeId: entry.startNodeId, authentication: { method: 'password' } };
            draft.nodes.push(node);
            entry.startNodeId = node.id;
            render();
        });
        const saveButton = button('Save route', async () => {
            if (offerRepair()) return;
            flush();
            routes.validate(draft);
            const result = await save(draft);
            dialog.close();
            onSaved(result, entry.id);
        });
        saveButton.className = 'primary';
        button('Cancel', () => dialog.close());
        const heading = document.createElement('div');
        heading.className = 'ssh-route-heading';
        for (const [text, field] of [['Entry name', name], ['Edit scope', scope]]) {
            const label = document.createElement('label');
            label.textContent = text;
            label.append(field);
            heading.append(label);
        }
        dialog.append(title, heading, rows, status, actions);
        dialog.addEventListener('close', () => dialog.remove());
        document.body.append(dialog);
        render();
        dialog.showModal();
    };
})();
