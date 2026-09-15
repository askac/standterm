(function() {
    'use strict';
    const routes = window.StandTermSshRoutes;

    routes.nodeFields = function({ parent, role, node, profiles, savedKeys, newKeys, keyAllowed,
        createKey, copyPublicKey, hostIdentity, editorId, onBusy, onChange }) {
        const fields = document.createElement('div');
        fields.className = 'ssh-route-fields';
        parent.append(fields);
        const input = (label, value, parent = fields) => {
            const wrapper = document.createElement('label');
            wrapper.textContent = label;
            const field = document.createElement('input');
            field.value = value;
            field.setAttribute('aria-label', `${role} ${label}`);
            wrapper.append(field);
            parent.append(wrapper);
            return field;
        };
        const host = input('Host', node.endpoint.host);
        const port = input('Port', node.endpoint.port);
        const username = input('Username', node.endpoint.username);
        port.inputMode = 'numeric';
        const auth = StandTermSshNodeAuth({ parent: fields, role, authentication: node.authentication,
            endpoint: () => ({ host: host.value, port: port.value, username: username.value }),
            profiles, savedKeys, newKeys, keyAllowed, createKey, copyPublicKey, onBusy, onChange
        });
        const identity = document.createElement('details');
        identity.className = 'ssh-host-identity';
        const identityTitle = document.createElement('summary');
        identityTitle.textContent = 'Host identity';
        identity.append(identityTitle);
        parent.append(identity);
        const alias = input('Host key alias (optional)', node.hostKeyAlias, identity);
        const identityBody = document.createElement('div');
        identity.append(identityBody);
        const identityControl = hostIdentity && StandTermSshHostIdentity({
            parent: identityBody, editorId, nodeId: node.id, request: hostIdentity.request,
            read: () => ({ ...routes.endpoint({ host: host.value, port: port.value, username: username.value }),
                host_key_alias: alias.value.trim(), terminal_id: hostIdentity.terminalId })
        });
        identity.ontoggle = () => { if (identity.open) identityControl?.inspect(); };
        for (const field of [host, port, username, alias]) field.addEventListener('input', () => {
            auth.update(); identityControl?.invalidate();
        });
        return { host, port, username, alias, auth, read: () => ({
            endpoint: { host: host.value, port: port.value, username: username.value },
            hostKeyAlias: alias.value.trim(), authentication: auth.read()
        }) };
    };

    routes.edit = function({ state, entryId, target, onDone, keys = [], temporaryKeys = [], saveRoute = false, keyAllowed = false,
        createKey, copyPublicKey, hostIdentity, mode = 'prepare', entryName = '', baseState = state }) {
        const original = routes.clone(baseState);
        const savedKeys = [...keys];
        const newKeys = new Map(temporaryKeys.map(record => [record.keyId, record]));
        const editorId = routes.id();
        let pendingGenerations = 0;
        let saving = false;
        const managed = mode === 'manage';
        let draft = routes.clone(state);
        let entry = [...draft.profiles, ...draft.history].find(item => item.id === entryId);
        if (!entry) {
            const node = { id: routes.id(), endpoint: { host: target.host || '', port: target.port || '22', username: target.username || '' }, nextNodeId: null,
                hostKeyAlias: '', authentication: { method: 'password' } };
            entry = { id: routes.id(), name: entryName, startNodeId: node.id,
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
        rows.className = 'ssh-route-cards';
        const preview = document.createElement('p');
        preview.className = 'ssh-route-preview';
        const status = document.createElement('p');
        status.setAttribute('role', 'status');
        const actions = document.createElement('div');
        actions.className = 'ssh-route-buttons ssh-route-actions';
        const button = (label, callback, parent = actions) => {
            const element = document.createElement('button');
            element.type = 'button';
            element.textContent = label;
            element.onclick = () => {
                if (saving) return;
                try { Promise.resolve(callback()).catch(fail); } catch (err) { fail(err); }
            };
            parent.append(element);
            return element;
        };
        const fail = err => { status.textContent = err.message || 'SSH route could not be saved.'; };
        let controls = [];
        let draggingIndex = null;
        let expandedIndex = path().length === 1 ? 0 : -1;
        const labelPath = path => `Core host → ${path.map(node => `${node.endpoint.username}@${node.endpoint.host}:${node.endpoint.port}`).join(' → ')}`;

        function path() { return routes.resolve(draft, entry.startNodeId).path; }

        function updateSaveState() {
            doneButton.disabled = saving || pendingGenerations > 0;
        }

        // Keep incomplete values inside the modal. Done validates the completed draft.
        function replaceDraftNode(index, replacement) {
            const result = routes.resolve(draft, entry.startNodeId);
            const current = result.path[index];
            if (scope.value === 'all') {
                draft.nodes[draft.nodes.findIndex(node => node.id === current.id)] = { ...replacement, id: current.id };
            } else {
                const prefix = result.error === 'cycle' ? result.path : result.path.slice(0, index + 1);
                const copies = prefix.map(node => ({ ...routes.clone(node), id: routes.id() }));
                const mapping = new Map(prefix.map((node, i) => [node.id, copies[i].id]));
                copies[index] = { ...replacement, id: copies[index].id };
                copies.forEach((node, i) => {
                    if (i < copies.length - 1 || result.error === 'cycle') {
                        node.nextNodeId = mapping.get(node.nextNodeId) || node.nextNodeId;
                    }
                });
                draft.nodes.push(...copies);
                entry.startNodeId = copies[0].id;
            }
        }

        function flush() {
            controls.forEach(control => control.card.classList.remove('invalid'));
            controls.map(control => control.read()).forEach((fields, index) => {
                const current = path()[index];
                const updated = { ...current, ...fields };
                if (JSON.stringify(updated) !== JSON.stringify(current)) replaceDraftNode(index, updated);
            });
            entry.name = name.value.trim();
            routes.project(draft);
        }

        function canChangeOrder() {
            const result = routes.resolve(draft, entry.startNodeId);
            if (result.error && result.error !== 'depth') {
                throw new Error('Select Done to review the invalid link before changing card order.');
            }
        }

        function replacePath(path, nextExpandedIndex = expandedIndex) {
            entry.startNodeId = routes.copyPath(draft, path);
            expandedIndex = nextExpandedIndex;
            routes.project(draft);
            render();
        }

        function expandCard(index) {
            expandedIndex = index;
            controls.forEach((control, i) => {
                control.body.hidden = i !== index;
                control.toggle.setAttribute('aria-expanded', String(i === index));
            });
        }

        function moveCard(from, to) {
            if (from === to) return;
            canChangeOrder();
            flush();
            const reordered = path();
            reordered.splice(to, 0, reordered.splice(from, 1)[0]);
            let nextExpandedIndex = expandedIndex;
            if (expandedIndex === from) nextExpandedIndex = to;
            else if (from < expandedIndex && to >= expandedIndex) nextExpandedIndex -= 1;
            else if (from > expandedIndex && to <= expandedIndex) nextExpandedIndex += 1;
            replacePath(reordered, nextExpandedIndex);
            controls[to].handle.focus();
        }

        function discardUnusedDraftNodes() {
            // Preserve all pre-existing nodes, including stored orphans and their tails.
            const keep = new Set(original.nodes.map(node => node.id));
            for (const item of [...draft.profiles, ...draft.history]) keep.add(item.startNodeId);
            const nodes = new Map(draft.nodes.map(node => [node.id, node]));
            for (const nodeId of keep) {
                const next = nodes.get(nodeId)?.nextNodeId;
                if (next) keep.add(next);
            }
            draft.nodes = draft.nodes.filter(node => keep.has(node.id));
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
                    status.textContent = 'Repair selected. Review the target, then select Done.';
                    render();
                }, status);
            }
            return true;
        }

        function addJumpNode() {
            canChangeOrder();
            flush();
            const current = path();
            current.splice(current.length - 1, 0, { id: routes.id(),
                endpoint: { host: '', port: '22', username: target.username || '' },
                hostKeyAlias: '', nextNodeId: null, authentication: { method: 'password' } });
            replacePath(current, current.length - 2);
            controls[current.length - 2].host.focus();
        }

        function render() {
            controls = [];
            rows.replaceChildren();
            const result = routes.resolve(draft, entry.startNodeId);
            const source = document.createElement('div');
            source.className = 'ssh-route-source';
            source.textContent = 'Core';
            rows.append(source);
            preview.textContent = labelPath(result.path);
            result.path.forEach((node, index) => {
                if (index === result.path.length - 1) {
                    const add = button('Add jump node', addJumpNode, rows);
                    add.className = 'ssh-route-add';
                }
                const fieldset = document.createElement('fieldset');
                const legend = document.createElement('legend');
                const role = index === result.path.length - 1 ? 'Target' : `Jump ${index + 1}`;
                legend.textContent = role;
                legend.className = 'ssh-route-accessible-label';
                fieldset.dataset.role = role;
                fieldset.append(legend);
                const nodeButtons = document.createElement('div');
                nodeButtons.className = 'ssh-route-card-header';
                const handle = button('≡', () => {}, nodeButtons);
                handle.className = 'ssh-route-drag';
                handle.setAttribute('aria-label', `Reorder ${role}`);
                handle.title = 'Drag to reorder, or use the arrow keys';
                const toggle = button('', () => expandCard(expandedIndex === index ? -1 : index), nodeButtons);
                toggle.className = 'ssh-route-card-toggle';
                toggle.setAttribute('aria-label', `Edit ${role}`);
                const roleLabel = document.createElement('strong');
                roleLabel.textContent = role;
                const summary = document.createElement('span');
                summary.className = 'ssh-route-card-summary';
                const authSummary = document.createElement('small');
                authSummary.className = 'ssh-route-card-auth';
                toggle.append(roleLabel, summary, authSummary);
                handle.draggable = true;
                handle.ondragstart = event => {
                    draggingIndex = index;
                    event.dataTransfer.effectAllowed = 'move';
                    event.dataTransfer.setData('text/plain', String(index));
                    event.dataTransfer.setDragImage(fieldset, 20, 20);
                };
                handle.ondragend = () => {
                    draggingIndex = null;
                    rows.querySelectorAll('.drag-over').forEach(card => card.classList.remove('drag-over'));
                };
                handle.onkeydown = event => {
                    const next = event.key === 'ArrowUp' ? index - 1 : event.key === 'ArrowDown' ? index + 1 : index;
                    if (next === index) return;
                    event.preventDefault();
                    if (next >= 0 && next < controls.length) {
                        try { moveCard(index, next); } catch (err) { fail(err); }
                    }
                };
                fieldset.ondragover = event => {
                    if (draggingIndex === null) return;
                    event.preventDefault();
                    event.dataTransfer.dropEffect = 'move';
                    fieldset.classList.add('drag-over');
                };
                fieldset.ondragleave = event => {
                    if (!fieldset.contains(event.relatedTarget)) fieldset.classList.remove('drag-over');
                };
                fieldset.ondrop = event => {
                    if (draggingIndex === null) return;
                    event.preventDefault();
                    const from = draggingIndex;
                    draggingIndex = null;
                    fieldset.classList.remove('drag-over');
                    try { moveCard(from, index); } catch (err) { fail(err); }
                };
                const earlier = button('↑', () => moveCard(index, index - 1), nodeButtons);
                earlier.setAttribute('aria-label', 'Move up');
                earlier.title = 'Move up';
                earlier.disabled = index === 0;
                const later = button('↓', () => moveCard(index, index + 1), nodeButtons);
                later.setAttribute('aria-label', 'Move down');
                later.title = 'Move down';
                later.disabled = index === result.path.length - 1;
                if (result.path.length > 1) {
                    const remove = button('×', () => {
                        canChangeOrder();
                        flush();
                        const remaining = path().filter((_, i) => i !== index);
                        const nextExpandedIndex = expandedIndex === index ? Math.min(index, remaining.length - 1)
                            : expandedIndex > index ? expandedIndex - 1 : expandedIndex;
                        replacePath(remaining, nextExpandedIndex);
                    }, nodeButtons);
                    remove.setAttribute('aria-label', 'Remove');
                    remove.title = `Remove ${role}`;
                }
                fieldset.append(nodeButtons);
                const body = document.createElement('div');
                body.className = 'ssh-route-card-body';
                body.id = `ssh-route-card-body-${index}`;
                toggle.setAttribute('aria-controls', body.id);
                fieldset.append(body);
                const nodeFields = routes.nodeFields({ parent: body, role, node, profiles: draft.profiles,
                    savedKeys, newKeys, keyAllowed, createKey, copyPublicKey, hostIdentity, editorId,
                    onBusy: delta => { pendingGenerations += delta; updateSaveState(); },
                    onChange: () => { updateSummary(); updateSaveState(); }
                });
                const { host, port, username, alias, auth } = nodeFields;
                const advanced = document.createElement('details');
                const advancedTitle = document.createElement('summary');
                advancedTitle.textContent = 'Advanced node settings';
                advanced.append(advancedTitle);
                body.append(advanced);
                const affected = document.createElement('p');
                affected.textContent = `Referenced by: ${routes.references(draft, node.id).map(item => item.name || 'This Entry').join(', ')}`;
                advanced.append(affected);
                controls.push({ card: fieldset, handle, host, advanced, body, toggle, read: nodeFields.read });
                const otherEntries = document.createElement('select');
                otherEntries.setAttribute('aria-label', `${role} Next route`);
                otherEntries.add(new Option('Choose the next route...', ''));
                draft.profiles.forEach(item => otherEntries.add(new Option(item.name, item.id)));
                advanced.append(otherEntries);
                const referenceButtons = document.createElement('div');
                referenceButtons.className = 'ssh-route-buttons';
                advanced.append(referenceButtons);
                for (const copy of [false, true]) {
                    button(copy ? 'Copy route after this node' : 'Reference route after this node', () => {
                        const selected = draft.profiles.find(item => item.id === otherEntries.value);
                        if (!selected) throw new Error('Choose a route to append.');
                        canChangeOrder();
                        flush();
                        const previous = routes.clone(draft);
                        try {
                            const selectedPath = routes.resolve(draft, selected.startNodeId);
                            if (copy && selectedPath.error && selectedPath.error !== 'depth') throw new Error('Repair the selected route before copying it.');
                            const nextId = copy ? routes.copyPath(draft, selectedPath.path) : selected.startNodeId;
                            replaceDraftNode(index, { ...path()[index], nextNodeId: nextId });
                            routes.project(draft);
                            status.textContent = 'Route appended. Review the final target, then select Done to check the links.';
                            render();
                        } catch (err) {
                            draft = previous;
                            entry = [...draft.profiles, ...draft.history].find(item => item.id === entry.id);
                            render();
                            throw err;
                        }
                    }, referenceButtons);
                }
                function updateSummary() {
                    const endpoint = `${username.value.trim() ? `${username.value.trim()}@` : ''}${host.value.trim() || 'Enter host'}:${port.value || '22'}`;
                    summary.textContent = `${endpoint}${alias.value.trim() ? ` · ${alias.value.trim()}` : ''}`;
                    authSummary.textContent = auth.read().method === 'password' ? 'Password' : auth.read().keyRef ? 'Key' : 'Key unavailable';
                    const aliasText = alias.value.trim() ? ` · Alias: ${alias.value.trim()}` : '';
                    toggle.title = `${role}: ${endpoint} · ${authSummary.textContent}${aliasText}`;
                }
                updateSummary();
                fieldset.addEventListener('input', () => {
                    fieldset.classList.remove('invalid');
                    updateSummary();
                    preview.textContent = labelPath(controls.map(control => control.read()));
                    status.replaceChildren();
                });
                rows.append(fieldset);
            });
            expandCard(expandedIndex < controls.length ? expandedIndex : -1);
            updateScopeNotice();
            updateSaveState();
        }

        const saveRouteLabel = document.createElement('label');
        const saveRouteInput = document.createElement('input');
        saveRouteInput.type = 'checkbox';
        saveRouteInput.checked = saveRoute;
        saveRouteLabel.className = 'ssh-key-toggle';
        saveRouteLabel.append(saveRouteInput, document.createTextNode('Save route on Connect'));
        saveRouteInput.setAttribute('aria-label', 'Save route');
        if (!managed) actions.append(saveRouteLabel);
        button('Cancel', () => dialog.close());
        const doneButton = button(managed ? 'Save route' : 'Done', async () => {
            if (pendingGenerations) return;
            flush();
            if (offerRepair()) return;
            const current = routes.checkedPath(draft, entry);
            current.forEach((node, index) => {
                let validEndpoint = false;
                try {
                    routes.endpoint(node.endpoint);
                    validEndpoint = true;
                    routes.publicNode(node);
                    if (node.authentication.method === 'browser-key' && !node.authentication.keyRef) {
                        throw new Error('Choose or create a browser key for this node.');
                    }
                } catch (err) {
                    const control = controls[index];
                    control.card.classList.add('invalid');
                    expandCard(index);
                    if (validEndpoint) control.advanced.open = true;
                    control.host.focus();
                    throw new Error(`${index === current.length - 1 ? 'Target' : `Jump ${index + 1}`}: ${err.message}`);
                }
            });
            if (!entry.name) {
                const endpoint = current.at(-1).endpoint;
                entry.name = `${endpoint.username.trim()}@${endpoint.host.trim()}`;
            }
            discardUnusedDraftNodes();
            routes.validate(draft);
            const used = new Set(draft.nodes.map(node => node.authentication.keyRef?.keyId));
            saving = true;
            updateSaveState();
            rows.inert = heading.inert = advanced.inert = true;
            status.textContent = managed ? 'Saving route…' : '';
            try {
                await onDone(draft, entry.id, [...newKeys.values()].filter(record => used.has(record.keyId)), saveRouteInput.checked);
                dialog.close();
            } finally {
                saving = false;
                rows.inert = heading.inert = advanced.inert = false;
                updateSaveState();
            }
        });
        doneButton.className = 'primary';
        const heading = document.createElement('div');
        heading.className = 'ssh-route-heading';
        for (const [text, field] of [['Entry name (optional)', name]]) {
            const label = document.createElement('label');
            label.textContent = text;
            label.append(field);
            heading.append(label);
        }
        const advanced = document.createElement('details');
        const advancedTitle = document.createElement('summary');
        advancedTitle.textContent = 'Advanced sharing';
        advanced.append(advancedTitle, scope);
        const scopeNotice = document.createElement('p');
        scopeNotice.hidden = true;
        function updateScopeNotice() {
            scopeNotice.hidden = scope.value !== 'all';
            scopeNotice.textContent = `Node edits also affect: ${[...new Set(path().flatMap(node => routes.references(draft, node.id).map(item => item.name || 'This Entry')))].join(', ')}. Reordering and removing cards only change this Entry.`;
        }
        scope.onchange = updateScopeNotice;
        const help = document.createElement('p');
        help.textContent = `Connect from Core through the cards, top to bottom. The last card is the Target. Drag the handle or use ↑ / ↓. Up to ${routes.MAX_JUMPS} jumps. ${managed
            ? 'Save route stores all nodes and selected keys. Changes apply to the next connection.'
            : 'Done returns to connection settings. Connect saves only when Save route is selected.'}`;
        dialog.append(title, help, heading, advanced, scopeNotice, rows, preview, status, actions);
        dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
        dialog.addEventListener('close', () => dialog.remove());
        document.body.append(dialog);
        render();
        dialog.showModal();
        return { addJump: addJumpNode };
    };
})();
