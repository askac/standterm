import json
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / 'site'
STORY = SITE / 'stories' / 'freebsd-build-worker' / 'index.html'


class PageParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.links = []
        self.json_ld = []
        self._in_json_ld = False
        self._json_parts = []
        self._in_story = False
        self._story_exclusion_depth = 0
        self.story_text = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        element_id = attributes.get('id')
        if element_id:
            self.ids.append(element_id)

        for name in ('href', 'src'):
            value = attributes.get(name)
            if value:
                self.links.append(value)

        if tag == 'article' and 'story' in attributes.get('class', '').split():
            self._in_story = True
        elif self._in_story and tag in ('pre', 'svg'):
            self._story_exclusion_depth += 1

        if tag == 'script' and attributes.get('type') == 'application/ld+json':
            self._in_json_ld = True
            self._json_parts = []

    def handle_endtag(self, tag):
        if self._in_json_ld and tag == 'script':
            self.json_ld.append(json.loads(''.join(self._json_parts)))
            self._in_json_ld = False
            self._json_parts = []

        if self._in_story and tag in ('pre', 'svg') and self._story_exclusion_depth:
            self._story_exclusion_depth -= 1
        elif tag == 'article':
            self._in_story = False

    def handle_data(self, data):
        if self._in_json_ld:
            self._json_parts.append(data)
        elif self._in_story and not self._story_exclusion_depth:
            self.story_text.append(data)


def parse_page(path):
    parser = PageParser()
    parser.feed(path.read_text(encoding='utf-8'))
    parser.close()
    return parser


def validate_local_links(path, parser):
    for link in parser.links:
        parsed = urlsplit(link)
        if parsed.scheme or parsed.netloc:
            continue
        if parsed.path.startswith('/standterm/'):
            target = SITE / parsed.path.removeprefix('/standterm/')
            if parsed.path.endswith('/'):
                target /= 'index.html'
            assert target.exists(), f'{path}: missing project-root target for {link}'
        elif parsed.path.startswith('/'):
            continue
        elif parsed.path:
            target = (path.parent / parsed.path).resolve()
            if parsed.path.endswith('/'):
                target /= 'index.html'
            assert target.exists(), f'{path}: missing local target for {link}'
        if parsed.fragment:
            assert parsed.fragment in parser.ids, (
                f'{path}: missing same-page anchor for {link}'
            )


def main():
    pages = [SITE / 'index.html', SITE / '404.html', STORY]
    parsed_pages = {}

    for path in pages:
        assert path.is_file(), f'missing static page: {path}'
        parser = parse_page(path)
        assert len(parser.ids) == len(set(parser.ids)), f'duplicate id in {path}'
        validate_local_links(path, parser)
        parsed_pages[path] = parser

    story_parser = parsed_pages[STORY]
    words = re.findall(r"\b[\w][\w'’+./-]*\b", ' '.join(story_parser.story_text))
    assert len(words) >= 20_000, f'story prose is only {len(words)} words'

    required_phrases = [
        'human-in-the-loop',
        'freebsd',
        'hyper-v',
        'external agent mirror',
        'boot beacon',
        'powershell',
        'bash inside wsl',
        'su',
    ]
    story_text = ' '.join(story_parser.story_text).lower()
    for phrase in required_phrases:
        assert phrase in story_text, f'missing required story phrase: {phrase}'

    site_text = '\n'.join(
        path.read_text(encoding='utf-8')
        for path in SITE.rglob('*')
        if path.is_file()
    )
    private_ipv4 = re.compile(
        r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}'
        r'|192\.168\.\d{1,3}\.\d{1,3}'
        r'|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b'
    )
    assert not private_ipv4.search(site_text), 'site contains a private IPv4 literal'
    assert not re.search(r'\b(?:fc|fd)[0-9a-f]{2}:[0-9a-f:]+', site_text, re.I), (
        'site contains an IPv6 unique-local address'
    )
    secret_markers = re.compile(
        r'-----BEGIN (?:OPENSSH |RSA |EC )?PRIVATE KEY-----'
        r'|\bgh[pousr]_[A-Za-z0-9]{20,}\b'
        r'|\bsk-[A-Za-z0-9_-]{20,}\b'
    )
    assert not secret_markers.search(site_text), 'site contains a secret-shaped value'

    assert story_parser.json_ld, 'story is missing Article JSON-LD'
    article = story_parser.json_ld[0]
    assert article['@type'] == 'TechArticle'
    assert article['wordCount'] >= 20_000
    assert abs(article['wordCount'] - len(words)) < 2_000

    sitemap = ET.parse(SITE / 'sitemap.xml')
    locations = {
        node.text
        for node in sitemap.findall('{http://www.sitemaps.org/schemas/sitemap/0.9}url/'
                                     '{http://www.sitemaps.org/schemas/sitemap/0.9}loc')
    }
    assert 'https://askac.github.io/standterm/' in locations
    assert 'https://askac.github.io/standterm/stories/freebsd-build-worker/' in locations

    print(f'static site smoke passed ({len(words)} prose words)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
