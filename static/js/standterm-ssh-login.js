(function() {
    'use strict';

    window.StandTermSshLogin = function({ terminalId, attemptId, route, send, cancel, back, isActive }) {
        const element = document.createElement('section');
        element.className = 'ssh-login-flow';
        const progress = document.createElement('div');
        progress.className = 'ssh-login-progress';
        progress.setAttribute('aria-label', 'SSH connection progress');
        const source = document.createElement('span');
        source.textContent = 'Core';
        source.dataset.phase = 'complete';
        progress.append(source);
        element.append(progress);
        const message = document.createElement('p');
        message.className = 'ssh-login-message';
        message.setAttribute('role', 'status');
        const actions = document.createElement('div');
        actions.className = 'ssh-login-actions';
        const button = (label, callback, parent) => {
            const value = document.createElement('button');
            value.type = 'button';
            value.textContent = label;
            value.onclick = callback;
            parent.append(value);
            return value;
        };
        const labels = { waiting: 'Waiting', connect: 'Connecting', forward: 'Opening next hop',
            verify_and_authenticate: 'Verifying host', local_keys: 'Trying local keys', host_key: 'Confirm host key',
            password: 'Requires password', authenticate: 'Authenticating', authenticated: 'OK',
            shell: 'Opening terminal', complete: 'OK', failed: 'Failed' };
        const nodes = route.map((node, index) => {
            const role = index === route.length - 1 ? 'Target' : `Node ${index + 1}`;
            const marker = document.createElement('span');
            marker.textContent = role;
            marker.dataset.phase = 'waiting';
            progress.append(document.createTextNode(' → '), marker);
            const card = document.createElement('section');
            card.className = 'ssh-login-card';
            card.dataset.nodeId = node.node_id;
            card.dataset.phase = 'waiting';
            const heading = document.createElement('div');
            heading.className = 'ssh-login-card-heading';
            const title = document.createElement('strong');
            title.textContent = role;
            const endpoint = document.createElement('span');
            endpoint.className = 'ssh-login-endpoint';
            endpoint.textContent = `${node.username}@${node.host}:${node.port}${node.host_key_alias ? ` · ${node.host_key_alias}` : ''}`;
            endpoint.title = endpoint.textContent;
            const status = document.createElement('span');
            status.className = 'ssh-login-card-status';
            status.textContent = labels.waiting;
            heading.append(title, endpoint, status);
            const body = document.createElement('div');
            body.className = 'ssh-login-card-body';
            body.hidden = true;
            card.append(heading, body);
            element.append(card);
            return { id: node.node_id, role, card, marker, status, body, phase: 'waiting', request: null };
        });
        element.append(message, actions);
        const cancelButton = button('Cancel connection', cancel, actions);
        const backButton = button('Back to connection settings', back, actions);
        backButton.hidden = true;
        let activeIndex = -1;
        let finished = false;
        const requests = new Set();

        function clearInput(node) {
            node.body.querySelectorAll('input').forEach(input => { input.value = ''; });
            node.request = null;
            node.body.replaceChildren();
            node.body.hidden = true;
        }

        function setPhase(node, phase) {
            node.phase = phase;
            node.card.dataset.phase = phase;
            node.marker.dataset.phase = phase;
            node.status.textContent = labels[phase];
        }

        function focus() {
            if (!isActive() || finished) return;
            const field = nodes[activeIndex]?.body.querySelector('input, [data-default-focus]');
            if (field && !field.disabled && field.isConnected) field.focus();
        }

        function handle(data) {
            if (finished || data.attempt_id !== attemptId || data.terminal_id !== terminalId) return false;
            const index = nodes.findIndex(node => node.id === data.node_id);
            if (index < activeIndex || index < 0 || data.hop !== index + 1 || data.total !== nodes.length) return false;
            const node = nodes[index];
            if (!Object.hasOwn(labels, data.phase)) return false;
            const prompt = data.message_type === 'ssh_login_prompt';
            if (prompt && (!['password', 'host_key'].includes(data.kind) || data.phase !== data.kind
                    || typeof data.request_id !== 'string' || requests.has(data.request_id))) return false;
            if (['authenticated', 'complete'].includes(node.phase) && data.phase !== 'shell') return false;
            activeIndex = index;
            clearInput(node);
            nodes.forEach((item, i) => { if (i !== index) clearInput(item); });
            setPhase(node, index === nodes.length - 1 && data.phase === 'authenticated' ? 'shell' : data.phase);
            if (!prompt) return true;
            requests.add(data.request_id);
            node.request = data.request_id;
            node.body.hidden = false;
            const explanation = document.createElement('p');
            explanation.textContent = data.message || '';
            node.body.append(explanation);
            const reply = fields => {
                if (finished || !isActive() || node.request !== data.request_id) return;
                const payload = { terminal_id: terminalId, attempt_id: attemptId, node_id: node.id,
                    request_id: data.request_id, kind: data.kind, ...fields };
                clearInput(node);
                setPhase(node, data.kind === 'password' ? 'authenticate' : 'verify_and_authenticate');
                send(payload);
            };
            if (data.kind === 'password') {
                const form = document.createElement('form');
                const label = document.createElement('label');
                label.textContent = 'Password';
                const input = document.createElement('input');
                input.type = 'password';
                input.autocomplete = 'off';
                input.setAttribute('aria-label', `${node.role} password`);
                label.append(input);
                form.append(label);
                const submit = button('Log in', () => {}, form);
                submit.type = 'submit';
                form.onsubmit = event => { event.preventDefault(); reply({ password: input.value }); };
                node.body.append(form);
            } else {
                const question = document.createElement('p');
                question.textContent = data.question || 'Remember this host key?';
                node.body.append(question);
                button('Trust and continue', () => reply({ accept: true }), node.body);
                button('Cancel connection', cancel, node.body).dataset.defaultFocus = 'true';
            }
            requestAnimationFrame(focus);
            return true;
        }

        function finish(success, text, context) {
            finished = true;
            nodes.forEach(clearInput);
            if (success) nodes.forEach(node => setPhase(node, 'complete'));
            else {
                const failed = nodes.find(node => node.id === context?.node_id) || nodes[Math.max(0, activeIndex)];
                if (failed) setPhase(failed, 'failed');
            }
            message.textContent = text || '';
            cancelButton.hidden = true;
            backButton.hidden = success;
        }

        return { element, handle, focus, finish, destroy() { nodes.forEach(clearInput); element.remove(); finished = true; } };
    };
})();
