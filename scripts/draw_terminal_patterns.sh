#!/usr/bin/env bash
set -euo pipefail

bar_width=""
clear_screen=1

usage() {
    cat <<'USAGE'
Usage: scripts/draw_terminal_patterns.sh [--width N] [--no-clear]

Draw a static ANSI terminal-rendering test card. The paired lower-half and
upper-half block rows are intended to reveal unintended horizontal seams.

Options:
  --width N     Set the test bar width. Default: fit the terminal, up to 96.
  --no-clear    Keep the existing terminal contents above the test card.
  -h, --help    Show this help.
USAGE
}

die() {
    printf '[!] ERROR: %s\n' "$*" >&2
    exit 1
}

is_positive_int() {
    case "${1:-}" in
        ''|*[!0-9]*)
            return 1
            ;;
        *)
            [ "$1" -gt 0 ]
            ;;
    esac
}

detect_width() {
    local stty_size
    if stty_size="$(stty size 2>/dev/null)"; then
        set -- $stty_size
        if [ "$#" -eq 2 ] && is_positive_int "$2"; then
            printf '%s\n' "$2"
            return
        fi
    fi

    if is_positive_int "${COLUMNS:-}"; then
        printf '%s\n' "$COLUMNS"
        return
    fi

    if command -v tput >/dev/null 2>&1; then
        local tput_cols
        if tput_cols="$(tput cols 2>/dev/null)" && is_positive_int "$tput_cols"; then
            printf '%s\n' "$tput_cols"
            return
        fi
    fi

    printf '80\n'
}

repeat_text() {
    local text="$1"
    local count="$2"
    local index
    for ((index = 0; index < count; index++)); do
        printf '%s' "$text"
    done
}

draw_glyph_row() {
    local label="$1"
    local color="$2"
    local glyph="$3"
    printf '%-22s%s' "$label" "$color"
    repeat_text "$glyph" "$bar_width"
    printf '%s\n' "$reset"
}

draw_progress() {
    local label="$1"
    local percent="$2"
    local filled=$((bar_width * percent / 100))
    local empty=$((bar_width - filled))
    printf '%-22s[' "$label"
    printf '%s' "$green"
    repeat_text '█' "$filled"
    printf '%s' "$dim"
    repeat_text '░' "$empty"
    printf '%s] %3d%%%s\n' "$reset" "$percent" "$reset"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --width)
            [ "$#" -ge 2 ] || die '--width requires a value'
            bar_width="$2"
            shift 2
            ;;
        --no-clear)
            clear_screen=0
            shift
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

terminal_width="$(detect_width)"
if [ -z "$bar_width" ]; then
    bar_width=$((terminal_width - 28))
    [ "$bar_width" -gt 96 ] && bar_width=96
fi
is_positive_int "$bar_width" || die '--width must be a positive integer'
[ "$bar_width" -ge 8 ] || die '--width must be at least 8'

reset=$'\033[0m'
bold=$'\033[1m'
dim=$'\033[38;5;240m'
cyan=$'\033[38;5;45m'
blue=$'\033[38;5;33m'
green=$'\033[38;5;82m'
yellow=$'\033[38;5;226m'
magenta=$'\033[38;5;201m'
blue_bg=$'\033[48;5;33m'
yellow_on_blue=$'\033[38;5;226;48;5;33m'
yellow_on_yellow=$'\033[38;5;226;48;5;226m'

if [ "$clear_screen" -eq 1 ]; then
    printf '\033[2J\033[H'
fi

printf '%sStandTerm terminal rendering test card%s\n' "$bold" "$reset"
printf 'Terminal: %s columns | Test bar: %s cells\n\n' "$terminal_width" "$bar_width"

printf '%s[1] Adjacent full-block rows%s (no dark seam expected)\n' "$bold" "$reset"
draw_glyph_row 'full block row 1' "$cyan" '█'
draw_glyph_row 'full block row 2' "$cyan" '█'
draw_glyph_row 'full block row 3' "$cyan" '█'
printf '\n'

printf '%s[2] Half-block boundary seam%s\n' "$bold" "$reset"
printf 'The GOOD pair should join at the row boundary; the CONTROL pair has an intentional dark band.\n'
draw_glyph_row 'GOOD top: lower half' "$yellow" '▄'
draw_glyph_row 'GOOD bottom: upper' "$yellow" '▀'
draw_glyph_row 'CONTROL top: upper' "$magenta" '▀'
draw_glyph_row 'CONTROL bottom: lower' "$magenta" '▄'
printf '\n'

printf '%s[3] Background-cell fill%s (geometry reference; no seam expected)\n' "$bold" "$reset"
draw_glyph_row 'background row 1' "$blue_bg" ' '
draw_glyph_row 'background row 2' "$blue_bg" ' '
draw_glyph_row 'background row 3' "$blue_bg" ' '
printf '\n'

printf '%s[4] Half blocks on explicit cell backgrounds%s\n' "$bold" "$reset"
printf 'Yellow should meet at the GOOD boundary; any exposed seam should be blue, not black.\n'
draw_glyph_row 'GOOD blue/yellow low' "$yellow_on_blue" '▄'
draw_glyph_row 'GOOD blue/yellow up' "$yellow_on_blue" '▀'
draw_glyph_row 'same-color lower' "$yellow_on_yellow" '▄'
draw_glyph_row 'same-color upper' "$yellow_on_yellow" '▀'
printf '\n'

printf '%s[5] Progress and fractional-width glyphs%s\n' "$bold" "$reset"
draw_progress 'progress 25%' 25
draw_progress 'progress 50%' 50
draw_progress 'progress 75%' 75
printf '%-22s%s' 'fraction ramp' "$blue"
repeat_text '▏▎▍▌▋▊▉█' "$((bar_width / 8))"
repeat_text '█' "$((bar_width % 8))"
printf '%s\n' "$reset"
draw_glyph_row 'lower half only' "$green" '▄'
draw_glyph_row 'upper half only' "$green" '▀'
printf '\n'

printf '%s[6] Box drawing, wide text, and ANSI colors%s\n' "$bold" "$reset"
printf 'single: ╭──────────────╮  double: ╔══════════════╗  heavy: ┏━━━━━━━━━━━━━━┓\n'
printf '        │ StandTerm    │          ║ StandTerm    ║         ┃ StandTerm    ┃\n'
printf '        ╰──────────────╯          ╚══════════════╝         ┗━━━━━━━━━━━━━━┛\n'
printf 'wide:   中文測試  日本語  한글  ＡＢＣ  emoji: 🟩🟨🟥\n'
printf 'colors: '
for color_index in 196 202 226 46 51 33 93 201; do
    printf '\033[48;5;%sm      %s' "$color_index" "$reset"
done
printf '\n%sEnd of static test card.%s\n' "$bold" "$reset"
