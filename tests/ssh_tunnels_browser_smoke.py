"""Check human tunnel controls, direction labels and stale SSH/UI replies."""
import agent_browser_smoke as fixture


def test_tunnel_controls(browser, url):
    context, page = fixture.new_page(browser, url)
    try:
        result = page.evaluate("""() => {
            const test = window.terminalTest;
            const button = document.getElementById('ssh-tunnels-btn');
            const hiddenForLocal = button.hidden;
            test.applyTerminalListForTest({ terminals: [{ terminal_id: 'main', connection_type: 'ssh',
                terminal_label: 'Tunnel fixture', connected: true, files_available: true }] });
            const visibleForSsh = !button.hidden;
            test.holdSshTunnelRequestsForTest();
            button.click();
            const dialog = document.getElementById('ssh-tunnels-dialog');
            test.completeSshTunnelRequestForTest(0, { status: 'ok', tunnels: [], connection_id: 'connection-1' });
            const direction = document.getElementById('ssh-tunnel-direction');
            direction.value = 'remote';
            direction.dispatchEvent(new Event('change'));
            const labels = {
                listen: document.getElementById('ssh-tunnel-listen-label').innerText,
                target: document.getElementById('ssh-tunnel-target-host-label').innerText,
                bind: document.getElementById('ssh-tunnel-bind-hint').innerText,
                lifetime: document.getElementById('ssh-tunnel-policy').innerText
            };
            const name = '<img src=x onerror=alert(1)>';
            document.getElementById('ssh-tunnel-name').value = name;
            document.getElementById('ssh-tunnel-target-host').value = '127.0.0.1';
            document.getElementById('ssh-tunnel-target-port').value = '8080';
            document.getElementById('ssh-tunnel-form').requestSubmit();
            const requests = () => test.getEmitted().filter(item => item.event === 'ssh_tunnel');
            const start = requests().at(-1).args[0];
            const snapshot = { tunnel_id: 'ssht_one', name, direction: 'remote', target_host: '127.0.0.1',
                listen_port: 0, target_port: 8080, bound_port: null, status: 'starting', revision: 1,
                connections: 0, bytes_sent: 0, bytes_received: 0 };
            test.completeSshTunnelRequestForTest(1, { status: 'ok', tunnel: snapshot });
            const update = tunnel => test.applySshTunnelStateForTest({
                terminal_id: 'main', connection_id: 'connection-1', tunnel });
            update({ ...snapshot, bound_port: 50001, status: 'listening', revision: 2, bytes_sent: 4096 });
            const row = document.querySelector('.ssh-tunnel-item');
            const running = row.innerText;
            const safeName = row.querySelector('.ssh-tunnel-name').textContent === name && !row.querySelector('img');
            test.applySshTunnelStateForTest({ terminal_id: 'main', connection_id: 'foreign',
                tunnel: { ...snapshot, status: 'failed', revision: 99 } });
            const foreignIgnored = row.innerText === running;
            row.querySelector('button').click();
            const stop = requests().at(-1).args[0];
            test.completeSshTunnelRequestForTest(2, { status: 'ok', tunnel: { ...snapshot, status: 'stopped', revision: 3 } });
            update({ ...snapshot, status: 'listening', revision: 2 });
            const stopped = row.querySelector('button').disabled && row.innerText.includes('Stopped');
            document.getElementById('ssh-tunnel-form').requestSubmit();
            dialog.close();
            test.completeSshTunnelRequestForTest(3, { status: 'ok', tunnel: { ...snapshot, tunnel_id: 'ssht_late' } });
            const lateIgnored = !dialog.querySelector('[data-tunnel-id="ssht_late"]');
            button.click();
            test.completeSshTunnelRequestForTest(4, { status: 'ok', tunnels: [{ ...snapshot, status: 'listening' }], connection_id: 'connection-2' });
            document.getElementById('ssh-tunnels-refresh').click();
            test.completeSshTunnelRequestForTest(5, { status: 'ok', tunnels: [], connection_id: 'connection-3' });
            const changedConnection = document.getElementById('ssh-tunnel-start').disabled
                && document.querySelector('.ssh-tunnel-item button').disabled
                && document.querySelector('.ssh-tunnel-details').textContent.includes('unavailable');
            return { hiddenForLocal, visibleForSsh, labels, start, stop, running, safeName, foreignIgnored, stopped, lateIgnored, changedConnection };
        }""")
        assert result['hiddenForLocal'] and result['visibleForSsh']
        assert result['labels']['listen'].startswith('Remote listening port')
        assert 'Core host' in result['labels']['target']
        assert 'actual listening interfaces' in result['labels']['bind']
        assert 'reload' in result['labels']['lifetime'] and 'WSL' in result['labels']['lifetime']
        assert result['start']['spec'] == {
            'direction': 'remote', 'listen_port': 0, 'target_host': '127.0.0.1', 'target_port': 8080,
            'name': '<img src=x onerror=alert(1)>',
        }
        assert result['start']['operation'] == 'start' and result['start']['terminal_id'] == 'main'
        assert result['stop']['operation'] == 'stop' and result['stop']['tunnel_id'] == 'ssht_one'
        assert 'SSH remote 127.0.0.1:50001' in result['running'] and 'Sent 4.00 KiB' in result['running'], result['running']
        assert all(result[key] for key in ('safeName', 'foreignIgnored', 'stopped', 'lateIgnored', 'changedConnection'))
    finally:
        fixture.close_context(context)


if __name__ == '__main__':
    server = None
    try:
        server, access_url = fixture.start_server()
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                test_tunnel_controls(browser, access_url)
                print('SSH tunnel browser controls: PASS')
            finally:
                browser.close()
    finally:
        if server:
            fixture.stop_server(server)
