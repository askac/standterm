"""Check human tunnel controls, direction labels and stale SSH/UI replies."""
import agent_browser_smoke as fixture


def translated(page, key, params=None):
    text = page.evaluate("""({key,params}) =>
        StandTermI18n.create(StandTermMessages, document.documentElement.lang).t(key, params)
    """, {'key':key,'params':params or {}})
    assert text != key, f'Missing reviewed translation: {key}'
    return text


def new_tunnel_page(browser, url, locale):
    context, page = fixture.new_page(browser, url, ui_language=locale)
    page.evaluate('() => window.terminalTest.captureTerminalIoForTest()')
    return context, page


def test_tunnel_controls(browser, url, locale='en'):
    context, page = new_tunnel_page(browser, url, locale)
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
            const startMessage = document.getElementById('ssh-tunnels-message').textContent;
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
            const stopMessage = document.getElementById('ssh-tunnels-message').textContent;
            update({ ...snapshot, status: 'listening', revision: 2 });
            const stopped = row.querySelector('button').disabled;
            const stoppedDetails = row.querySelector('.ssh-tunnel-details').textContent;
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
                && !!document.querySelector('.ssh-tunnel-details').textContent;
            const unavailable = document.querySelector('.ssh-tunnel-details').textContent;
            const staleMessage = document.getElementById('ssh-tunnels-message').textContent;
            const beforeStaleActions = requests().length;
            document.getElementById('ssh-tunnel-form').requestSubmit();
            document.querySelector('.ssh-tunnel-item button').dispatchEvent(new Event('click'));
            const staleActionsIgnored = requests().length === beforeStaleActions;
            return { hiddenForLocal, visibleForSsh, labels, start, stop, running, safeName, foreignIgnored,
                stopped, stoppedDetails, startMessage, stopMessage, lateIgnored, changedConnection, unavailable, staleMessage, staleActionsIgnored };
        }""")
        assert result['hiddenForLocal'] and result['visibleForSsh']
        for field, key in {'listen':'remote_listen','target':'remote_target_host','bind':'remote_bind','lifetime':'policy'}.items():
            assert result['labels'][field] == translated(page, 'ssh.tunnels.' + key)
        assert result['unavailable'] == translated(page, 'ssh.tunnels.viewer_unavailable')
        assert result['staleMessage'] == translated(page, 'ssh.tunnels.view_changed')
        assert result['startMessage'] == translated(page, 'ssh.tunnels.start_requested')
        assert result['stopMessage'] == translated(page, 'ssh.tunnels.stop_requested')
        assert result['stoppedDetails'].startswith(translated(page, 'ssh.tunnels.status_stopped') + ' · ')
        assert result['start']['spec'] == {
            'direction': 'remote', 'listen_port': 0, 'target_host': '127.0.0.1', 'target_port': 8080,
            'name': '<img src=x onerror=alert(1)>',
        }
        assert result['start']['operation'] == 'start' and result['start']['terminal_id'] == 'main'
        assert result['stop'] == {'terminal_id':'main','operation':'stop','tunnel_id':'ssht_one'}
        assert '127.0.0.1:50001' in result['running'] and '4.00 KiB' in result['running'], result['running']
        assert all(result[key] for key in ('safeName', 'foreignIgnored', 'stopped', 'lateIgnored', 'changedConnection', 'staleActionsIgnored'))
    finally:
        fixture.close_context(context)


def test_directions_statuses_and_close_preserve_protocol(browser, url, locale='en'):
    context, page = new_tunnel_page(browser, url, locale)
    try:
        result = page.evaluate("""() => {
            const test = window.terminalTest;
            test.applyTerminalListForTest({terminals:[{terminal_id:'main',connection_type:'ssh',
                terminal_label:'SSH <i>& {title}',connected:true,files_available:true}]});
            test.holdSshTunnelRequestsForTest(); test.clearEmitted();
            document.getElementById('ssh-tunnels-btn').click();
            test.completeSshTunnelRequestForTest(0,{status:'ok',tunnels:[],connection_id:'direction-connection'});
            const requests = () => test.getEmitted().filter(item => item.event === 'ssh_tunnel');
            const results = [];
            for (const [index,direction] of ['local','remote'].entries()) {
                const selector = document.getElementById('ssh-tunnel-direction');
                const fields = ['ssh-tunnel-listen','ssh-tunnel-target-host','ssh-tunnel-target-port']
                    .map(id => document.getElementById(id));
                const oldValues = fields.map(field => field.value);
                selector.value = direction; selector.dispatchEvent(new Event('change'));
                const inputsPreserved = fields.every((field,index) => document.getElementById(field.id) === field && field.value === oldValues[index]);
                document.getElementById('ssh-tunnel-name').value = `Name ${direction} <b>& {name}`;
                document.getElementById('ssh-tunnel-listen').value = index ? '55001' : '0';
                document.getElementById('ssh-tunnel-target-host').value = ' 2001:db8::1 ';
                document.getElementById('ssh-tunnel-target-port').value = '8443';
                document.getElementById('ssh-tunnel-form').requestSubmit();
                const request = requests().at(-1).args[0];
                const tunnel = {tunnel_id:`ssht_${direction}`,name:request.spec.name,direction,
                    listen_port:request.spec.listen_port,bound_port:null,target_host:request.spec.target_host,
                    target_port:8443,status:'starting',revision:1,connections:2,bytes_sent:4096,bytes_received:1024};
                test.completeSshTunnelRequestForTest(index+1,{status:'ok',tunnel});
                const row = document.querySelector(`[data-tunnel-id="${tunnel.tunnel_id}"]`);
                const statuses = [];
                for (const [offset,status] of ['starting','listening','failed','stopped','Listening'].entries()) {
                    test.applySshTunnelStateForTest({terminal_id:'main',connection_id:'direction-connection',
                        tunnel:{...tunnel,status,revision:offset+2,cleanup_pending:true,error:'Raw <em>& {error}'}});
                    statuses.push({status,details:row.querySelector('.ssh-tunnel-details').textContent,
                        stopDisabled:row.querySelector('button').disabled});
                }
                results.push({direction,request,statuses,inputsPreserved,
                    listen:document.getElementById('ssh-tunnel-listen-label').innerText,
                    target:document.getElementById('ssh-tunnel-target-host-label').innerText,
                    bind:document.getElementById('ssh-tunnel-bind-hint').innerText,
                    name:row.querySelector('.ssh-tunnel-name').textContent,
                    error:row.querySelector('.ssh-tunnel-error').textContent,
                    route:row.querySelector('.ssh-tunnel-route').textContent,
                    html:row.querySelectorAll('b,em').length});
                test.applySshTunnelStateForTest({terminal_id:'main',connection_id:'direction-connection',
                    tunnel:{...tunnel,name:'',status:'listening',revision:8,bound_port:55002}});
                results.at(-1).defaultName = row.querySelector('.ssh-tunnel-name').textContent;
            }
            return {directions:[...document.getElementById('ssh-tunnel-direction').options].map(option => ({value:option.value,label:option.text})),
                results,carrier:document.getElementById('ssh-tunnels-carrier').textContent,
                carrierHtml:document.getElementById('ssh-tunnels-carrier').querySelectorAll('i').length};
        }""")
        assert [item['value'] for item in result['directions']] == ['local','remote']
        assert [item['label'] for item in result['directions']] == [
            translated(page, 'ssh.tunnels.local_direction'),translated(page, 'ssh.tunnels.remote_direction')]
        assert '<i>& {title}' in result['carrier'] and result['carrierHtml'] == 0
        assert result['carrier'] == translated(page, 'ssh.tunnels.carrier', {'title':'SSH <i>& {title}','id':'main'})
        for element_id, key in {'ssh-tunnels-title':'ssh.tunnels.title','ssh-tunnel-start':'ssh.tunnels.start',
                                'ssh-tunnels-refresh':'ssh.tunnels.refresh','ssh-tunnels-close':'common.close'}.items():
            assert page.locator('#' + element_id).inner_text() == translated(page, key)
        for entry in result['results']:
            direction = entry['direction']
            assert entry['inputsPreserved'], 'Changing direction replaced controls or reset entered values'
            for field, suffix in {'listen':'listen','target':'target_host','bind':'bind','defaultName':'name'}.items():
                assert entry[field] == translated(page, f'ssh.tunnels.{direction}_{suffix}')
            assert entry['request'] == {'terminal_id':'main','operation':'start','spec':{
                'name':f'Name {direction} <b>& {{name}}','direction':direction,
                'listen_port':0 if direction == 'local' else 55001,'target_host':'2001:db8::1','target_port':8443}}
            assert entry['name'] == entry['request']['spec']['name'] and entry['html'] == 0
            assert entry['error'] == 'Raw <em>& {error}'
            assert entry['route'] == translated(page, f'ssh.tunnels.{direction}_route', {
                'port':translated(page, 'ssh.tunnels.automatic') if direction == 'local' else 55001,
                'host':'[2001:db8::1]','target_port':8443})
            for status in entry['statuses']:
                assert status['stopDisabled'] == (status['status'] not in ['starting','listening'])
                status_key = 'unknown' if status['status'] == 'Listening' else status['status']
                expected_details = translated(page, 'ssh.tunnels.details', {
                    'status':translated(page, 'ssh.tunnels.status_' + status_key),
                    'connections':2,'sent':'4.00 KiB','received':'1.00 KiB'})
                assert status['details'] == expected_details + ' · ' + translated(page, 'ssh.tunnels.cleanup_pending')
        page.set_viewport_size({'width':480,'height':600})
        layout = page.locator('#ssh-tunnels-dialog').evaluate("""dialog => {
            const bounds = dialog.getBoundingClientRect();
            return {inside:bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0 && bounds.bottom <= innerHeight,
                fits:dialog.scrollWidth <= dialog.clientWidth+1,
                fields:[...dialog.querySelectorAll('input,select')].every(element => {
                    const rect = element.getBoundingClientRect(); return rect.left >= bounds.left && rect.right <= bounds.right;
                })};
        }""")
        assert all(layout.values()), layout
        before = fixture.get_emitted(page, 'ssh_tunnel')
        page.click('#ssh-tunnels-close')
        page.wait_for_selector('#ssh-tunnels-dialog', state='hidden')
        assert not any(event['args'][0]['operation'] == 'stop' for event in fixture.get_emitted(page, 'ssh_tunnel'))
        assert len([event for event in fixture.get_emitted(page, 'ssh_tunnel') if event['args'][0]['operation'] == 'start']) == 2
        assert len(before) >= 3
    finally:
        fixture.close_context(context)


def test_timeout_keeps_status_polling_without_repeating_start(browser, url, locale='en'):
    context, page = new_tunnel_page(browser, url, locale)
    try:
        result = page.evaluate("""() => {
            const test = window.terminalTest;
            test.applyTerminalListForTest({terminals:[{terminal_id:'main',connection_type:'ssh',
                terminal_label:'Timeout fixture',connected:true,files_available:true}]});
            test.holdSshTunnelRequestsForTest(); test.clearEmitted();
            const timeouts = [], polls = [];
            const originalTimeout = window.setTimeout, originalInterval = window.setInterval;
            let timerId = 1000000;
            window.setTimeout = (callback,delay,...args) => delay === 15000
                ? (timeouts.push(callback), ++timerId) : originalTimeout(callback,delay,...args);
            window.setInterval = (callback,delay,...args) => delay === 2000
                ? (polls.push(callback), ++timerId) : originalInterval(callback,delay,...args);
            try {
                document.getElementById('ssh-tunnels-btn').click();
                test.completeSshTunnelRequestForTest(0,{status:'ok',tunnels:[],connection_id:'timeout-connection'});
                document.getElementById('ssh-tunnel-form').requestSubmit();
                const requests = () => test.getEmitted().filter(item => item.event === 'ssh_tunnel').map(item => item.args[0]);
                const beforeTimeout = requests();
                timeouts[1]();
                const timeoutMessage = document.getElementById('ssh-tunnels-message').textContent;
                const afterTimeout = requests();
                polls[0]();
                const afterPoll = requests();
                test.completeSshTunnelRequestForTest(1,{status:'ok',tunnel:{tunnel_id:'ssht_late',revision:1}});
                const lateIgnored = !document.querySelector('[data-tunnel-id="ssht_late"]');
                test.completeSshTunnelRequestForTest(2,{status:'error',message:'Raw <b>& {message}'});
                const rawError = document.getElementById('ssh-tunnels-message').textContent;
                const rawHtml = document.querySelectorAll('#ssh-tunnels-message b').length;
                polls[0]();
                test.completeSshTunnelRequestForTest(3,{status:'error'});
                const fallback = document.getElementById('ssh-tunnels-message').textContent;
                document.getElementById('ssh-tunnels-close').click();
                return {beforeTimeout,afterTimeout,afterPoll,timeoutMessage,lateIgnored,rawError,rawHtml,fallback,
                    operations:requests().map(request => request.operation)};
            } finally {
                window.setTimeout = originalTimeout; window.setInterval = originalInterval;
            }
        }""")
        assert [item['operation'] for item in result['beforeTimeout']] == ['status','start']
        assert result['afterTimeout'] == result['beforeTimeout'], 'Timeout automatically resent a tunnel mutation'
        assert [item['operation'] for item in result['afterPoll']] == ['status','start','status']
        assert result['operations'] == ['status','start','status','status']
        assert result['lateIgnored'] and result['rawHtml'] == 0
        assert result['rawError'] == 'Raw <b>& {message}'
        assert result['timeoutMessage'] == translated(page, 'ssh.tunnels.timeout')
        assert result['fallback'] == translated(page, 'ssh.tunnels.request_failed')
    finally:
        fixture.close_context(context)


if __name__ == '__main__':
    server = None
    try:
        server, access_url = fixture.start_server()
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for locale in ['en','zh-TW']:
                    for test in [test_tunnel_controls,test_directions_statuses_and_close_preserve_protocol,
                                 test_timeout_keeps_status_polling_without_repeating_start]:
                        test(browser, access_url, locale)
                        print(f'{test.__name__} ({locale}): PASS', flush=True)
            finally:
                browser.close()
    finally:
        if server:
            fixture.stop_server(server)
