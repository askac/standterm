"""Check localized remote Agent access without changing grants or machine-facing data."""
import json

import agent_browser_smoke as fixture


REMOTE_URL = 'http://127.0.0.1:43210/agentinfo?fixture=raw&value=%7Burl%7D'
REMOTE_PROMPT = 'Run discover, then hello.\nAgentInfoURL: ' + REMOTE_URL + '\nLiteral <b>& {prompt}'
SSH_CONTEXT = {'host':'SSH <img>& {context}','ssh_tab':'main','command':'discover hello'}


def message(page, key, params=None):
    value = page.evaluate("""({key,params}) =>
        StandTermI18n.create(StandTermMessages, document.documentElement.lang).t(key, params)
    """, {'key':key,'params':params or {}})
    assert value != key, f'Missing reviewed translation: {key}'
    return value


def ready_payload():
    return {'status':'ready','carrier_id':'fixture-carrier','agentinfo_url':REMOTE_URL,
            'verified_at':1700000000,'connect_info':REMOTE_PROMPT,'ssh_context':SSH_CONTEXT,
            'terminal_ids':['main'],'terminals':[{'terminal_id':'main','last_request_at':None}]}


def assert_no_permission_mutations(page):
    assert not any(event['event'] in {'agent_attach','agent_detach','agent_mode_set','agent_pause'}
                   for event in fixture.get_emitted(page)), 'Tunnel controls changed Agent Panel permissions'


def test_remote_status_boundaries_preserve_permissions_and_literals(browser, url, ui_language='en'):
    context, page = fixture.new_page(browser, url, ui_language=ui_language)
    token_requests = []
    page.on('request', lambda request: token_requests.append(request.url)
            if '/agent/external/token' in request.url else None)
    try:
        fixture.attach_agent(page)
        page.evaluate("""() => {
            window.terminalTest.captureTerminalIoForTest();
            window.terminalTest.holdAgentTunnelRequestsForTest();
            window.terminalTest.applyTerminalListForTest({terminals:[
                {terminal_id:'main',connected:true,connection_type:'ssh',terminal_label:'Host <b>& {title}'},
                {terminal_id:'disabled-tab',connected:true,connection_type:'ssh',terminal_label:'No permission'}]});
            window.terminalTest.clearEmitted();
        }""")
        page.click('#agent-tunnel-btn')
        assert page.inner_text('#agent-tunnel-message') == message(page, 'agent.tunnel.updating')
        assert page.inner_text('#agent-tunnel-carrier') == message(page, 'agent.tunnel.carrier_tab', {
            'title':'Host <b>& {title}','id':'main'})
        ready = ready_payload()
        page.evaluate('payload => window.terminalTest.completeAgentTunnelRequestForTest(0,payload)', ready)
        for requested, mode, permission_key in [
                (None,'observe','observe'),('approval','approval_pending','approval'),
                ('direct','direct_active','direct'),('pause','paused','paused')]:
            if requested == 'pause':
                fixture.emit_socket(page, 'agent_pause', {'terminal_id':'main'})
                fixture.wait_for_agent(page, "state.mode === 'paused'")
            elif requested:
                fixture.set_agent_mode(page, requested, mode)
            before = fixture.active_agent_state(page)
            page.evaluate('() => window.terminalTest.clearEmitted()')
            page.evaluate('payload => window.terminalTest.applyAgentTunnelStatusForTest(payload)', ready)
            permission = message(page, 'agent.permission.' + permission_key)
            target = message(page, 'agent.tunnel.target_row', {'title':'Host <b>& {title}','id':'main','permission':permission})
            assert page.inner_text('#agent-tunnel-targets') == target + ' · ' + message(page, 'agent.tunnel.remote_access_ready')
            assert mode not in page.inner_text('#agent-tunnel-targets')
            assert page.locator('#agent-tunnel-targets b, #agent-tunnel-targets input').count() == 0
            assert 'No permission' not in page.inner_text('#agent-tunnel-targets')
            assert page.inner_text('#agent-tunnel-message') == message(page, 'agent.tunnel.ready')
            assert page.inner_text('#agent-tunnel-activity') == 'main: ' + message(page, 'agent.connection.waiting')
            after = fixture.active_agent_state(page)
            assert after['mode'] == mode and after['external_token'] == before['external_token']
            assert_no_permission_mutations(page)
        assert page.input_value('#agent-tunnel-url') == REMOTE_URL
        assert page.input_value('#agent-tunnel-info') == REMOTE_PROMPT
        assert page.inner_text('#agent-tunnel-carrier') == message(page, 'agent.tunnel.carrier_context', {
            'context':json.dumps(SSH_CONTEXT, separators=(',',':'))})
        assert page.locator('#agent-tunnel-carrier img').count() == 0
        for element_id in ['agent-tunnel-title','agent-tunnel-copy','agent-tunnel-apply','agent-tunnel-stop',
                           'agent-tunnel-check','agent-tunnel-refresh','agent-tunnel-close']:
            element = page.locator('#' + element_id)
            key = element.get_attribute('data-i18n')
            assert key and element.inner_text() == message(page, key)
        for element_id in ['agent-tunnel-url','agent-tunnel-info']:
            element = page.locator('#' + element_id)
            key = element.get_attribute('data-i18n-aria-label')
            assert key and element.get_attribute('aria-label') == message(page, key)
        for key in ['agent.tunnel.lifecycle_hint','agent.tunnel.check_hint']:
            hint = page.locator(f'#agent-tunnel-dialog [data-i18n="{key}"]')
            assert hint.is_visible() and hint.inner_text() == message(page, key)

        page.set_viewport_size({'width':480,'height':600})
        layout = page.locator('#agent-tunnel-dialog').evaluate("""dialog => {
            const box = dialog.getBoundingClientRect();
            return {inside:box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight,
                fits:dialog.scrollWidth <= dialog.clientWidth+1,
                controls:[...dialog.querySelectorAll('input,textarea,button')].filter(element => element.getClientRects().length)
                    .every(element => {const rect = element.getBoundingClientRect(); return rect.left >= box.left && rect.right <= box.right;})};
        }""")
        assert all(layout.values()), layout
        page.set_viewport_size({'width':1280,'height':800})

        no_grants = dict(ready, terminal_ids=[], terminals=[])
        page.evaluate('payload => window.terminalTest.applyAgentTunnelStatusForTest(payload)', no_grants)
        assert page.input_value('#agent-tunnel-info') == REMOTE_PROMPT
        assert page.inner_text('#agent-tunnel-activity') == message(page, 'agent.connection.no_grants')
        assert message(page, 'agent.tunnel.remote_access_ready') not in page.inner_text('#agent-tunnel-targets')
        page.evaluate('() => window.terminalTest.clearEmitted()')
        page.click('#agent-tunnel-close')
        page.wait_for_selector('#agent-tunnel-dialog', state='hidden')
        assert not fixture.get_emitted(page, 'agent_tunnel'), 'Closing the ready tunnel sent an operation'
        assert page.locator('#agent-connect-btn').is_visible()

        page.click('#agent-tunnel-btn')
        page.evaluate('payload => window.terminalTest.completeAgentTunnelRequestForTest(1,payload)', ready)
        page.click('#agent-tunnel-stop')
        requests = fixture.get_emitted(page, 'agent_tunnel')
        assert [event['args'][0] for event in requests] == [
            {'terminal_id':'main','operation':'status'},{'terminal_id':'main','operation':'stop'}]
        page.evaluate("""() => window.terminalTest.completeAgentTunnelRequestForTest(2,
            {status:'stopped',cleanup_pending:true})""")
        assert page.inner_text('#agent-tunnel-message') == message(page, 'agent.tunnel.cleanup_pending')
        assert page.input_value('#agent-tunnel-info') == '' and page.input_value('#agent-tunnel-url') == ''
        assert page.locator('#agent-tunnel-copy').is_hidden() and page.locator('#agent-tunnel-stop').is_disabled()
        assert page.locator('#agent-connect-btn').is_hidden()
        for payload, key in [({'status':'stopped','cleanup_pending':False},'agent.tunnel.stopped'),
                             (None,'agent.tunnel.setup_failed'),
                             ({'status':message(page, 'agent.tunnel.stopped')},'agent.tunnel.setup_failed')]:
            page.evaluate('payload => window.terminalTest.applyAgentTunnelStatusForTest(payload)', payload)
            assert page.inner_text('#agent-tunnel-message') == message(page, key)
        diagnostic = 'Remote diagnostic <img>& {message}\nRAW_CODE'
        page.evaluate('payload => window.terminalTest.applyAgentTunnelStatusForTest(payload)', {
            'status':'error','message':diagnostic})
        assert page.inner_text('#agent-tunnel-message') == diagnostic
        assert page.locator('#agent-tunnel-message img').count() == 0
        assert_no_permission_mutations(page)
        assert not token_requests, 'Tunnel setup or status display minted a local Agent token'
        assert fixture.active_agent_state(page)['mode'] == 'paused'
    finally:
        fixture.close_context(context)


def main():
    proc, url = fixture.start_server()
    try:
        with fixture.load_playwright()[0]() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for locale in ['en','zh-TW']:
                    for test in [fixture.test_agent_tunnel_uses_panel_permissions_and_keeps_focus,
                                 fixture.test_remote_agent_info_tracks_ssh_carrier_and_rejects_late_replies,
                                 test_remote_status_boundaries_preserve_permissions_and_literals]:
                        test(browser, url, ui_language=locale)
                        print(f'{test.__name__} ({locale}): PASS', flush=True)
            finally:
                browser.close()
    finally:
        fixture.stop_server(proc)


if __name__ == '__main__':
    main()
