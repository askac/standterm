#!/usr/bin/env bash
set -euo pipefail

protocol=all

usage() {
    cat <<'USAGE'
Usage: bash scripts/image-check.sh [--protocol all|sixel|iterm2]

Draw a 240x96 image test card using SIXEL and iTerm2 inline PNG output.
Run inside a StandTerm Local Shell tab with shell images enabled. Both cards
should show RGB panels, a white circular outline, and a checkerboard strip.
The circle should be round, and text below each image should remain readable.

Options:
  --protocol NAME  Select all (default), sixel, or iterm2.
  -h, --help       Show this help without emitting image sequences.

Requires Bash and Python 3; uses only the Python standard library.
No downloads, files, terminal queries, or screen clearing are performed.
This is a visual check; the script cannot detect whether images are displayed.
USAGE
}

die() {
    printf '[!] ERROR: %s\n' "$*" >&2
    exit 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --protocol)
            [ "$#" -ge 2 ] || die '--protocol requires a value'
            protocol="$2"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            die "unknown option: $1"
            ;;
    esac
done

case "$protocol" in
    all|sixel|iterm2) ;;
    *) die '--protocol must be all, sixel, or iterm2' ;;
esac
command -v python3 >/dev/null 2>&1 || die 'Python 3 is required (no extra modules needed)'

python3 - "$protocol" <<'PY'
import base64
import struct
import sys
import zlib

WIDTH, HEIGHT = 240, 96
PALETTE = [(16, 24, 32), (255, 255, 255), (255, 0, 0), (0, 255, 0), (0, 0, 255)]
output = sys.stdout.buffer


def text(value):
    output.write(value.encode('ascii'))


def color_at(x, y):
    if x < 2 or x >= WIDTH - 2 or y < 2 or y >= HEIGHT - 2:
        return 1
    if y >= HEIGHT * 3 // 4:
        return (x // 8 + y // 8) % 2
    distance = (x - WIDTH // 2) ** 2 + (y - HEIGHT * 3 // 8) ** 2
    if 18 ** 2 <= distance <= 21 ** 2:
        return 1
    return 2 + min(2, x * 3 // WIDTH)


pixels = [[color_at(x, y) for x in range(WIDTH)] for y in range(HEIGHT)]


def sixel_runs(values):
    runs = []
    previous, count = values[0], 0
    for value in values:
        if value != previous:
            runs.append(('!' + str(count) if count > 1 else '') + chr(63 + previous))
            previous, count = value, 0
        count += 1
    runs.append(('!' + str(count) if count > 1 else '') + chr(63 + previous))
    return ''.join(runs)


def draw_sixel():
    text('\x1bPq"1;1;' + str(WIDTH) + ';' + str(HEIGHT))
    for index, color in enumerate(PALETTE):
        text('#{};2;{};{};{}'.format(index, *(round(channel * 100 / 255) for channel in color)))
    for top in range(0, HEIGHT, 6):
        for color in range(len(PALETTE)):
            values = [sum(1 << bit for bit in range(min(6, HEIGHT - top))
                          if pixels[top + bit][x] == color) for x in range(WIDTH)]
            if any(values):
                text('#' + str(color) + sixel_runs(values) + '$')
        if top + 6 < HEIGHT:
            text('-')
    text('\x1b\\')


def draw_iterm2():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))

    raw = b''.join(b'\x00' + bytes(channel for color in row for channel in PALETTE[color]) for row in pixels)
    image = (b'\x89PNG\r\n\x1a\n'
             + chunk(b'IHDR', struct.pack('>IIBBBBB', WIDTH, HEIGHT, 8, 2, 0, 0, 0))
             + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    text(f'\x1b]1337;File=inline=1;size={len(image)};width={WIDTH}px;height={HEIGHT}px;preserveAspectRatio=1:')
    output.write(base64.b64encode(image))
    text('\x07')


text('StandTerm inline image check\r\n')
text('Expected: RGB panels, a round white outline, and a checkerboard strip (240x96 px).\r\n')
text('Enable local shell images in Settings > General, then save.\r\n\r\n')
for protocol, name, draw in [('sixel', 'SIXEL', draw_sixel), ('iterm2', 'iTerm2 PNG', draw_iterm2)]:
    if sys.argv[1] not in ('all', protocol):
        continue
    text(name + '\r\n')
    draw()
    text('\r\nTEXT AFTER ' + name + ': this line should appear below the image.\r\n\r\n')
text('Visual check complete. Disable shell images, save, and rerun to compare.\r\n')
output.flush()
PY
