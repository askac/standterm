"""Check opt-in image delivery without granting terminal or agent access."""

import base64
from unittest.mock import patch

import agent_backend_smoke as fixture
from scripts.image_preview import MAX_REQUEST_BYTES, image_metadata, validate_preview


standterm = fixture.standterm


def main():
    fixture.reset_state()
    standterm.image_preview_viewers.clear()
    payload = {'title': '<img src=x onerror=alert(1)>',
               'image_base64': fixture.make_test_png_base64(32, 24)}
    sender = standterm.app.test_client()
    first = fixture.make_client()
    second = fixture.make_client()
    try:
        assert sender.post('/preview/image', json=payload).status_code == 409
        first.emit('image_preview_set', {'enabled': True})
        first.get_received()
        second.get_received()
        result = sender.post('/preview/image', json=payload)
        assert result.status_code == 200, result.json
        assert result.json['status'] == 'sent' and result.json['viewers'] == 1
        assert 'image_base64' not in result.json['preview']
        events = [event['args'][0] for event in first.get_received() if event['name'] == 'image_preview']
        assert len(events) == 1 and events[0]['title'] == payload['title']
        assert events[0]['width'] == 32 and events[0]['height'] == 24
        assert not any(event['name'] == 'image_preview' for event in second.get_received())
        assert not standterm.agent_states and not standterm.external_agent_attach_store._tokens

        assert sender.post('/preview/image', json=payload, headers={'Origin': 'https://example.org'}).status_code == 403
        assert sender.post('/preview/image', json=payload, environ_overrides={'REMOTE_ADDR': '192.0.2.1'}).status_code == 403
        for invalid in [{}, [], {'image_base64': '%%%'} , {'image_base64': base64.b64encode(b'<svg/>').decode()},
                        {**payload, 'title': 'x' * 161}, {'image_base64': fixture.make_test_png_base64(4097, 4096)}]:
            assert sender.post('/preview/image', json=invalid).status_code == 400
        assert sender.post('/preview/image', data=b'x' * (MAX_REQUEST_BYTES + 1),
                           content_type='application/json').status_code == 413
        first.emit('image_preview_set', {'enabled': False})
        assert sender.post('/preview/image', json=payload).status_code == 409
        first.emit('image_preview_set', {'enabled': 'true'})
        assert sender.post('/preview/image', json=payload).status_code == 409
        first.emit('image_preview_set', {'enabled': True})
        first.disconnect()
        assert sender.post('/preview/image', json=payload).status_code == 409

        with patch.object(standterm, 'HTTPS_ENABLED', True):
            second.emit('image_preview_set', {'enabled': True})
            assert sender.post('/preview/image', json=payload).status_code == 409
            sid = fixture.current_sid_for_session(fixture.current_session_token())
            standterm.socket_browser_authorized[sid] = True
            second.emit('image_preview_set', {'enabled': True})
            assert sender.post('/preview/image', json=payload).status_code == 200
            standterm.socket_browser_authorized[sid] = False
            assert sender.post('/preview/image', json=payload).status_code == 409

        jpeg = b'\xff\xd8\xff\xc0\x00\x0b\x08\x00\x18\x00\x20\x01\x01\x11\x00\xff\xd9'
        assert image_metadata(jpeg)['mime_type'] == 'image/jpeg'
        assert validate_preview(payload)['byte_length'] > 0
        print('Image preview: opt-in, viewer isolation, limits, origins and browser authorization: PASS')
    finally:
        if first.is_connected():
            first.disconnect()
        second.disconnect()
        standterm.image_preview_viewers.clear()


if __name__ == '__main__':
    main()
