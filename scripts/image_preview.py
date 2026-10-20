#!/usr/bin/env python3
"""Send a bounded image to opted-in StandTerm pages without terminal input."""

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import struct
import urllib.error
import urllib.parse
import urllib.request


MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096
MAX_TITLE_LENGTH = 160
MAX_REQUEST_BYTES = (MAX_IMAGE_BYTES + 2) // 3 * 4 + 8192


def image_metadata(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('Image must be between 1 byte and 2 MiB.')
    if data.startswith(b'\x89PNG\r\n\x1a\n') and len(data) >= 33:
        if data[8:16] != b'\x00\x00\x00\rIHDR':
            raise ValueError('Invalid PNG header.')
        width, height = struct.unpack('>II', data[16:24])
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8'):
        offset = 2
        mime = None
        while offset < len(data):
            if data[offset] != 0xff:
                raise ValueError('Invalid JPEG marker.')
            while offset < len(data) and data[offset] == 0xff:
                offset += 1
            if offset >= len(data):
                break
            marker = data[offset]
            offset += 1
            if marker in (0xd9, 0xda):
                break
            if marker == 0x01 or 0xd0 <= marker <= 0xd7:
                continue
            if offset + 2 > len(data):
                break
            size = int.from_bytes(data[offset:offset + 2], 'big')
            if size < 2 or offset + size > len(data):
                break
            if marker in (0xc0, 0xc1, 0xc2):
                if size < 8:
                    break
                height, width = struct.unpack('>HH', data[offset + 3:offset + 7])
                mime = 'image/jpeg'
                break
            offset += size
        if mime is None:
            raise ValueError('JPEG dimensions are missing.')
    else:
        raise ValueError('Only PNG and JPEG images are supported.')
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError('Image dimensions exceed the 16 megapixel limit.')
    return {'mime_type': mime, 'width': width, 'height': height, 'byte_length': len(data)}


def validate_preview(payload):
    if not isinstance(payload, dict):
        raise ValueError('Image preview must be a JSON object.')
    title = payload.get('title', '')
    encoded = payload.get('image_base64')
    if not isinstance(title, str) or len(title) > MAX_TITLE_LENGTH:
        raise ValueError('Image title exceeds 160 characters.')
    if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise ValueError('Image payload exceeds the 2 MiB limit.')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError('Invalid image base64.') from exc
    metadata = image_metadata(data)
    return {**metadata, 'title': title, 'image_base64': base64.b64encode(data).decode('ascii')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('image', type=Path, help='PNG or JPEG on the helper machine')
    parser.add_argument('--url', help='Exact StandTerm origin, including scheme and port')
    parser.add_argument('--agentinfo', help='Known tokenless Agent Info URL or JSON path; no agent token is needed')
    parser.add_argument('--ca-file', help='CA bundle for HTTPS')
    parser.add_argument('--title', help='Preview title; defaults to the filename')
    args = parser.parse_args()
    # Reuse tokenless discovery and TLS helpers, without loading a handoff/token.
    from agent_cli import build_ssl_context, is_url, load_agentinfo
    if not args.url and not args.agentinfo:
        current = None
        if hasattr(os, 'getuid'):
            current = Path(os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')) / 'standterm/current_agentinfo.json'
        if current and current.is_file():
            args.agentinfo = str(current)
        else:
            parser.error('Provide --url or --agentinfo from the running StandTerm instance.')
    try:
        if args.agentinfo:
            if is_url(args.agentinfo):
                info_url = args.agentinfo.rstrip('/')
                if not info_url.endswith('/agentinfo'):
                    info_url += '/agentinfo'
                discovery = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    urllib.request.HTTPSHandler(context=build_ssl_context(ca_file=args.ca_file)))
                with discovery.open(info_url, timeout=15) as response:
                    info = json.load(response)
                if not isinstance(info, dict):
                    raise ValueError('Agent Info must be a JSON object.')
            else:
                info = load_agentinfo(args.agentinfo, ca_file=args.ca_file)
            args.url = args.url or info.get('base_url')
            args.ca_file = args.ca_file or info.get('tls_ca_cert_path')
        origin = urllib.parse.urlsplit(args.url or '')
        if origin.scheme not in {'http', 'https'} or not origin.hostname or origin.username or origin.password \
                or origin.path not in ('', '/') or origin.query or origin.fragment:
            raise ValueError('Use only the StandTerm origin, without paths, queries or tokens.')
        # Preview delivery is local to Core, including when the page uses a LAN URL.
        url = urllib.parse.urlunsplit((origin.scheme, f'127.0.0.1:{origin.port or (443 if origin.scheme == "https" else 80)}', '/preview/image', '', ''))
        with args.image.open('rb') as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
        payload = validate_preview({'title': args.title if args.title is not None else args.image.name,
                                    'image_base64': base64.b64encode(data).decode('ascii')})
        request = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'),
                                         headers={'Content-Type': 'application/json'}, method='POST')
        # Loopback must never go through an environment-configured HTTP proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                    urllib.request.HTTPSHandler(context=build_ssl_context(ca_file=args.ca_file)))
        with opener.open(request, timeout=15) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        try:
            result = json.load(exc)
        except (ValueError, OSError):
            result = {'status': 'failed', 'error_code': f'http_{exc.code}'}
    except (OSError, ValueError, urllib.error.URLError) as exc:
        result = {'status': 'failed', 'message': str(exc)}
    print(json.dumps(result, ensure_ascii=True))
    return 0 if result.get('status') == 'sent' else 1


if __name__ == '__main__':
    raise SystemExit(main())
