(function() {
    'use strict';

    window.StandTermSshHostIdentity = function({ parent, read, request, editorId, nodeId, translate }) {
        const t = (key, fallback, params = {}) => {
            const translated = typeof translate === 'function' ? translate(key, params) : key;
            if (translated !== key) return translated;
            return fallback.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g,
                (placeholder, name) => Object.hasOwn(params, name) ? String(params[name]) : placeholder);
        };
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
        const refresh = button(t('ssh.host_identity.check', 'Check saved fingerprint'), () => perform('inspect'));
        const forget = button(t('ssh.host_identity.forget', 'Forget saved fingerprint…'), () => perform('prepare_forget'));
        function invalidate() {
            revision += 1;
            pendingAction = null;
            confirmation.replaceChildren();
            status.textContent = t('ssh.host_identity.hint', 'Check the saved fingerprint for this host identity.');
            refresh.disabled = false;
            forget.hidden = true;
        }
        function show(result) {
            const fingerprints = Array.isArray(result.fingerprints) ? result.fingerprints : [];
            status.textContent = `${result.identity}\n${fingerprints.length ? fingerprints.join('\n') : t('ssh.host_identity.no_fingerprint', 'No saved fingerprint.')}\n${t('ssh.host_identity.verify_on_connect', 'The server will be verified when connecting.')}`;
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
            status.textContent = t('ssh.host_identity.checking', 'Checking host identity…');
            try {
                const result = await request(payload);
                if (!parent.isConnected || revision !== token || JSON.stringify(read()) !== snapshot) return;
                if (!result || result.request_id !== payload.request_id || result.editor_id !== editorId || result.node_id !== nodeId) {
                    throw new Error(t('ssh.host_identity.stale', 'SSH host identity reply is stale. Check again.'));
                }
                if (result.status === 'failed') throw new Error(result.message || t('ssh.host_identity.unavailable', 'SSH host identity is unavailable.'));
                if (result.status === 'confirm') {
                    pendingAction = result.action_id;
                    status.textContent = result.message;
                    const question = document.createElement('p');
                    question.textContent = result.question;
                    confirmation.append(question);
                    button(t('ssh.host_identity.forget_now', 'Forget now'), () => { if (pendingAction === result.action_id) perform('confirm', pendingAction); }, confirmation);
                    const cancel = button(t('ssh.host_identity.keep', 'Keep fingerprint'), () => { if (pendingAction === result.action_id) perform('cancel', pendingAction); }, confirmation);
                    cancel.focus();
                } else {
                    pendingAction = null;
                    if (result.status === 'cancelled') { invalidate(); perform('inspect'); }
                    else show(result);
                }
            } catch (err) {
                if (parent.isConnected && revision === token) status.textContent = err.message || t('ssh.host_identity.unavailable', 'SSH host identity is unavailable.');
            } finally {
                if (revision === token) { refresh.disabled = false; forget.disabled = false; }
            }
        }
        invalidate();
        return { invalidate, inspect: () => perform('inspect') };
    };
})();
