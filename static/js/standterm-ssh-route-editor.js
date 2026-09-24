(function() {
    'use strict';
    const routes = window.StandTermSshRoutes;

    routes.nodeFields = function({ parent, role, node, profiles, savedKeys, newKeys, keyAllowed,
        createKey, copyPublicKey, hostIdentity, editorId, onBusy, onChange, translate }) {
        const t = (key, fallback, params = {}) => {
            const translated = typeof translate === 'function' ? translate(key, params) : key;
            if (translated !== key) return translated;
            return fallback.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
                (placeholder, name) => Object.hasOwn(params, name) ? String(params[name]) : placeholder);
        };
        const fields = document.createElement('div');
        fields.className = 'ssh-route-fields';
        parent.append(fields);
        const input = (label, value, parent = fields) => {
            const wrapper = document.createElement('label');
            wrapper.textContent = label;
            const field = document.createElement('input');
            field.value = value;
            field.setAttribute('aria-label', t('ssh.editor.field_for_node', '{role} {label}', { role, label }));
            wrapper.append(field);
            parent.append(wrapper);
            return field;
        };
        const host = input(t('connection.host', 'Host'), node.endpoint.host);
        const port = input(t('connection.port', 'Port'), node.endpoint.port);
        const username = input(t('connection.username', 'Username'), node.endpoint.username);
        port.inputMode = 'numeric';
        const auth = StandTermSshNodeAuth({ parent: fields, role, authentication: node.authentication,
            endpoint: () => ({ host: host.value, port: port.value, username: username.value }),
            profiles, savedKeys, newKeys, keyAllowed, createKey, copyPublicKey, onBusy, onChange, translate
        });
        const identity = document.createElement('details');
        identity.className = 'ssh-host-identity';
        const identityTitle = document.createElement('summary');
        identityTitle.textContent = t('connection.host_identity', 'Host identity');
        identity.append(identityTitle);
        parent.append(identity);
        const alias = input(t('connection.host_alias', 'Host key alias (optional)'), node.hostKeyAlias, identity);
        const identityBody = document.createElement('div');
        identity.append(identityBody);
        const identityControl = hostIdentity && StandTermSshHostIdentity({
            parent: identityBody, editorId, nodeId: node.id, request: hostIdentity.request, translate,
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
        createKey, copyPublicKey, hostIdentity, mode = 'prepare', entryName = '', baseState = state, translate }) {
        const t = (key, fallback, params = {}) => {
            const translated = typeof translate === 'function' ? translate(key, params) : key;
            if (translated !== key) return translated;
            return fallback.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
                (placeholder, name) => Object.hasOwn(params, name) ? String(params[name]) : placeholder);
        };
        const original = routes.clone(baseState);
        const savedKeys = [...keys];
        const newKeys = new Map(temporaryKeys.map(record => [record.keyId, record]));
        const editorId = routes.id();
        let pendingGenerations = 0;
        let saving = false;
        const managed = mode === 'manage';
        const completionLabel = managed ? t('ssh.editor.save_route', 'Save route') : t('ssh.editor.done', 'Done');
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
        title.textContent = t('ssh.editor.title', 'SSH route');
        const name = document.createElement('input');
        name.value = entry.name || '';
        name.placeholder = t('ssh.editor.entry_name', 'Entry name');
        name.maxLength = 64;
        name.setAttribute('aria-label', t('ssh.editor.entry_name', 'Entry name'));
        const scope = document.createElement('select');
        scope.setAttribute('aria-label', t('ssh.editor.scope', 'Edit scope'));
        for (const [value, label] of [['entry', t('ssh.editor.scope_entry', 'Only this Entry')],
            ['all', t('ssh.editor.scope_all', 'Apply node edits to all references')]]) {
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
        const fail = err => { status.textContent = err.message || t('ssh.editor.save_failed', 'SSH route could not be saved.'); };
        let controls = [];
        let draggingIndex = null;
        let expandedIndex = path().length === 1 ? 0 : -1;
        const labelPath = path => `${t('ssh.editor.core_host', 'Core host')} → ${path.map(node => `${node.endpoint.username}@${node.endpoint.host}:${node.endpoint.port}`).join(' → ')}`;

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
                throw new Error(t('ssh.editor.review_before_reorder', 'Select {action} to review the invalid link before changing card order.', { action: completionLabel }));
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
                t('ssh.editor.cycle_rejected', '{path} → {host}: cycle rejected. Choose an explicit target or cancel.',
                    { path: labelPath(result.path), host: result.path[result.cycleIndex].endpoint.host })));
            for (const candidate of routes.repairCandidates(draft, entry)) {
                const target = candidate.path.at(-1).endpoint;
                const ruleLabel = candidate.rule === 'erase-loop'
                    ? t('ssh.editor.remove_loop', 'Remove loop') : t('ssh.editor.stop_before_back_edge', 'Stop before back edge');
                button(t('ssh.editor.repair_candidate', '{rule}: {path}; target {target}',
                    { rule: ruleLabel, path: labelPath(candidate.path), target: `${target.username}@${target.host}:${target.port}` }), () => {
                    // Keep other Entries and shared nodes exactly as they were before this edit.
                    const repaired = routes.clone(original);
                    let selected = [...repaired.profiles, ...repaired.history].find(item => item.id === entry.id);
                    if (!selected) { selected = { ...entry }; repaired.profiles.push(selected); }
                    selected.name = name.value.trim();
                    selected.startNodeId = routes.copyPath(repaired, candidate.path);
                    draft = routes.project(repaired);
                    entry = selected;
                    status.textContent = t('ssh.editor.repair_selected', 'Repair selected. Review the target, then select {action}.', { action: completionLabel });
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
                    const add = button(t('ssh.editor.add_jump', 'Add jump node'), addJumpNode, rows);
                    add.className = 'ssh-route-add';
                }
                const fieldset = document.createElement('fieldset');
                const legend = document.createElement('legend');
                const role = index === result.path.length - 1 ? 'Target' : `Jump ${index + 1}`;
                const displayRole = index === result.path.length - 1 ? t('ssh.login.target', 'Target')
                    : t('ssh.editor.jump', 'Jump {number}', { number: index + 1 });
                legend.textContent = displayRole;
                legend.className = 'ssh-route-accessible-label';
                fieldset.dataset.role = role;
                fieldset.append(legend);
                const nodeButtons = document.createElement('div');
                nodeButtons.className = 'ssh-route-card-header';
                const handle = button('≡', () => {}, nodeButtons);
                handle.className = 'ssh-route-drag';
                handle.setAttribute('aria-label', t('ssh.editor.reorder_node', 'Reorder {role}', { role: displayRole }));
                handle.title = t('ssh.editor.reorder_hint', 'Drag to reorder, or use the arrow keys');
                const toggle = button('', () => expandCard(expandedIndex === index ? -1 : index), nodeButtons);
                toggle.className = 'ssh-route-card-toggle';
                toggle.setAttribute('aria-label', t('ssh.editor.edit_node', 'Edit {role}', { role: displayRole }));
                const roleLabel = document.createElement('strong');
                roleLabel.textContent = displayRole;
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
                earlier.setAttribute('aria-label', t('ssh.editor.move_up', 'Move up'));
                earlier.title = t('ssh.editor.move_up', 'Move up');
                earlier.disabled = index === 0;
                const later = button('↓', () => moveCard(index, index + 1), nodeButtons);
                later.setAttribute('aria-label', t('ssh.editor.move_down', 'Move down'));
                later.title = t('ssh.editor.move_down', 'Move down');
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
                    remove.setAttribute('aria-label', t('ssh.editor.remove', 'Remove'));
                    remove.title = t('ssh.editor.remove_node', 'Remove {role}', { role: displayRole });
                }
                fieldset.append(nodeButtons);
                const body = document.createElement('div');
                body.className = 'ssh-route-card-body';
                body.id = `ssh-route-card-body-${index}`;
                toggle.setAttribute('aria-controls', body.id);
                fieldset.append(body);
                const nodeFields = routes.nodeFields({ parent: body, role: displayRole, node, profiles: draft.profiles,
                    savedKeys, newKeys, keyAllowed, createKey, copyPublicKey, hostIdentity, editorId, translate,
                    onBusy: delta => { pendingGenerations += delta; updateSaveState(); },
                    onChange: () => { updateSummary(); updateSaveState(); }
                });
                const { host, port, username, alias, auth } = nodeFields;
                const advanced = document.createElement('details');
                const advancedTitle = document.createElement('summary');
                advancedTitle.textContent = t('ssh.editor.advanced_node', 'Advanced node settings');
                advanced.append(advancedTitle);
                body.append(advanced);
                const affected = document.createElement('p');
                affected.textContent = t('ssh.editor.references', 'Referenced by: {entries}',
                    { entries: routes.references(draft, node.id).map(item => item.name || t('ssh.editor.this_entry', 'This Entry')).join(', ') });
                advanced.append(affected);
                controls.push({ card: fieldset, handle, host, advanced, body, toggle, read: nodeFields.read });
                const otherEntries = document.createElement('select');
                otherEntries.setAttribute('aria-label', t('ssh.editor.next_route_for_node', '{role} Next route', { role: displayRole }));
                otherEntries.add(new Option(t('ssh.editor.choose_next_route', 'Choose the next route...'), ''));
                draft.profiles.forEach(item => otherEntries.add(new Option(item.name, item.id)));
                advanced.append(otherEntries);
                const referenceButtons = document.createElement('div');
                referenceButtons.className = 'ssh-route-buttons';
                advanced.append(referenceButtons);
                const referenceHint = document.createElement('p');
                referenceHint.textContent = t('ssh.editor.append_hint', 'Replaces this node\'s following route. Copied nodes keep their key references.');
                advanced.append(referenceHint);
                for (const copy of [false, true]) {
                    button(copy ? t('ssh.editor.copy_route', 'Copy route after this node') : t('ssh.editor.reference_route', 'Reference route after this node'), () => {
                        const selected = draft.profiles.find(item => item.id === otherEntries.value);
                        if (!selected) throw new Error(t('ssh.editor.choose_route_to_append', 'Choose a route to append.'));
                        canChangeOrder();
                        flush();
                        const previous = routes.clone(draft);
                        try {
                            const selectedPath = routes.resolve(draft, selected.startNodeId);
                            if (copy && selectedPath.error && selectedPath.error !== 'depth') throw new Error(t('ssh.editor.repair_before_copy', 'Repair the selected route before copying it.'));
                            const nextId = copy ? routes.copyPath(draft, selectedPath.path) : selected.startNodeId;
                            replaceDraftNode(index, { ...path()[index], nextNodeId: nextId });
                            routes.project(draft);
                            status.textContent = t('ssh.editor.route_appended', 'Route appended. Review the final target, then select {action} to check the links.', { action: completionLabel });
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
                    const endpoint = `${username.value.trim() ? `${username.value.trim()}@` : ''}${host.value.trim() || t('ssh.editor.enter_host', 'Enter host')}:${port.value || '22'}`;
                    summary.textContent = `${endpoint}${alias.value.trim() ? ` · ${alias.value.trim()}` : ''}`;
                    authSummary.textContent = auth.read().method === 'password' ? t('ssh.editor.password', 'Password')
                        : auth.read().keyRef ? t('ssh.editor.key', 'Key') : t('ssh.editor.key_unavailable', 'Key unavailable');
                    const aliasText = alias.value.trim() ? ` · ${t('ssh.editor.alias', 'Alias: {alias}', { alias: alias.value.trim() })}` : '';
                    toggle.title = `${displayRole}: ${endpoint} · ${authSummary.textContent}${aliasText}`;
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
        saveRouteLabel.append(saveRouteInput, document.createTextNode(t('ssh.editor.save_on_connect', 'Save route on Connect')));
        saveRouteInput.setAttribute('aria-label', t('ssh.editor.save_route', 'Save route'));
        if (!managed) actions.append(saveRouteLabel);
        button(t('common.cancel', 'Cancel'), () => dialog.close());
        const doneButton = button(completionLabel, async () => {
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
                        throw new Error(t('ssh.editor.key_required', 'Choose or create a browser key for this node.'));
                    }
                } catch (err) {
                    const control = controls[index];
                    control.card.classList.add('invalid');
                    expandCard(index);
                    if (validEndpoint) control.advanced.open = true;
                    control.host.focus();
                    const displayRole = index === current.length - 1 ? t('ssh.login.target', 'Target')
                        : t('ssh.editor.jump', 'Jump {number}', { number: index + 1 });
                    throw new Error(`${displayRole}: ${err.message}`);
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
            status.textContent = managed ? t('ssh.editor.saving', 'Saving route…') : '';
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
        for (const [text, field] of [[t('ssh.editor.entry_name_optional', 'Entry name (optional)'), name]]) {
            const label = document.createElement('label');
            label.textContent = text;
            label.append(field);
            heading.append(label);
        }
        const advanced = document.createElement('details');
        const advancedTitle = document.createElement('summary');
        advancedTitle.textContent = t('ssh.editor.advanced_sharing', 'Advanced sharing');
        advanced.append(advancedTitle, scope);
        const scopeNotice = document.createElement('p');
        scopeNotice.hidden = true;
        function updateScopeNotice() {
            scopeNotice.hidden = scope.value !== 'all';
            scopeNotice.textContent = t('ssh.editor.scope_notice', 'Node edits also affect: {entries}. Reordering and removing cards only change this Entry.',
                { entries: [...new Set(path().flatMap(node => routes.references(draft, node.id).map(item => item.name || t('ssh.editor.this_entry', 'This Entry'))))].join(', ') });
        }
        scope.onchange = updateScopeNotice;
        const help = document.createElement('p');
        help.textContent = t('ssh.editor.order_help', 'Connect from Core through the cards, top to bottom. The last card is the Target. Drag the handle or use ↑ / ↓. Up to {max} jumps.', { max: routes.MAX_JUMPS })
            + ' ' + (managed
                ? t('ssh.editor.manage_help', 'Save route stores all nodes and selected keys. Changes apply to the next connection.')
                : t('ssh.editor.prepare_help', 'Done updates the connection draft without saving. Connect saves only when Save route is selected.'));
        dialog.append(title, help, heading, advanced, scopeNotice, rows, preview, status, actions);
        dialog.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
        dialog.addEventListener('close', () => dialog.remove());
        document.body.append(dialog);
        render();
        dialog.showModal();
        return { addJump: addJumpNode };
    };
})();
