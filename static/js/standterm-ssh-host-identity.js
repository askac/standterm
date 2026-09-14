(function() {
    'use strict';

    window.StandTermSshHostIdentity = function({ parent, read, request, editorId, nodeId }) {
        const status = document.createElement('p');
        status.className = 'ssh-host-identity-status';
        status.setAttribute('role', 'status');
        const actions = document.createElement('div');
        actions.className = 'ssh-route-buttons';
        const confirmation = document.createElement('div');
        confirmation.className = 'ssh-host-identity-confirm';
        parent.append(status, actions, confirmation);
        let revision = 0;
        let pendingAction = null;
        const button = (label, callback, container = actions) => {
            const value = document.createElement('button');
            value.type = 'button';
            value.textContent = label;
            value.onclick = callback;
            container.append(value);
            return value;
        };
        const refresh = button('Check saved fingerprint', () => perform('inspect'));
        const forget = button('Forget saved fingerprint…', () => perform('prepare_forget'));
        function invalidate() {
            revision += 1;
            pendingAction = null;
            confirmation.replaceChildren();
            status.textContent = 'Check the saved fingerprint for this host identity.';
            refresh.disabled = false;
            forget.hidden = true;
        }
        function show(result) {
            const fingerprints = Array.isArray(result.fingerprints) ? result.fingerprints : [];
            status.textContent = `${result.identity}\n${fingerprints.length ? fingerprints.join('\n') : 'No saved fingerprint.'}\nThe server will be verified when connecting.`;
            forget.hidden = !fingerprints.length;
        }
        async function perform(operation, action = null) {
            let values;
            try { values = read(); } catch (err) { status.textContent = err.message; return; }
            const snapshot = JSON.stringify(values);
            const token = ++revision;
            const payload = { ...values, editor_id: editorId, node_id: nodeId,
                request_id: StandTermSshRoutes.id(), operation, ...(action ? { action_id: action } : {}) };
            refresh.disabled = true;
            forget.disabled = true;
            confirmation.replaceChildren();
            status.textContent = 'Checking host identity…';
            try {
                const result = await request(payload);
                if (!parent.isConnected || revision !== token || JSON.stringify(read()) !== snapshot) return;
                if (!result || result.request_id !== payload.request_id || result.editor_id !== editorId || result.node_id !== nodeId) {
                    throw new Error('SSH host identity reply is stale. Check again.');
                }
                if (result.status === 'failed') throw new Error(result.message || 'SSH host identity is unavailable.');
                if (result.status === 'confirm') {
                    pendingAction = result.action_id;
                    status.textContent = result.message;
                    const question = document.createElement('p');
                    question.textContent = result.question;
                    confirmation.append(question);
                    button('Forget now', () => { if (pendingAction === result.action_id) perform('confirm', pendingAction); }, confirmation);
                    const cancel = button('Keep fingerprint', () => { if (pendingAction === result.action_id) perform('cancel', pendingAction); }, confirmation);
                    cancel.focus();
                } else {
                    pendingAction = null;
                    if (result.status === 'cancelled') { invalidate(); perform('inspect'); }
                    else show(result);
                }
            } catch (err) {
                if (parent.isConnected && revision === token) status.textContent = err.message || 'SSH host identity is unavailable.';
            } finally {
                if (revision === token) { refresh.disabled = false; forget.disabled = false; }
            }
        }
        invalidate();
        return { invalidate, inspect: () => perform('inspect') };
    };
})();
