#!/usr/bin/env bash
set -euo pipefail

rows=6
width=""
no_final_newline=0

usage() {
    cat <<'USAGE'
Usage: scripts/draw_terminal_box.sh [--rows N] [--width N] [--no-final-newline]

Draw an ASCII box whose line length exactly matches the detected terminal
column count. This is useful for checking off-by-one terminal width and final
column wrapping behavior.

Options:
  --rows N              Box height. Default: 6.
  --width N             Override detected terminal width.
  --no-final-newline    Do not print a newline after the bottom border.
  -h, --help            Show this help.
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

repeat_char() {
    local char="$1"
    local count="$2"
    local i
    for ((i = 0; i < count; i++)); do
        printf '%s' "$char"
    done
}

draw_border() {
    local newline="${1:-1}"
    if [ "$cols" -eq 2 ]; then
        printf '++'
    else
        printf '+'
        repeat_char '-' "$((cols - 2))"
        printf '+'
    fi
    if [ "$newline" -eq 1 ]; then
        printf '\n'
    fi
}

draw_inner() {
    if [ "$cols" -eq 2 ]; then
        printf '||\n'
    else
        printf '|'
        repeat_char ' ' "$((cols - 2))"
        printf '|\n'
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --rows)
            [ "$#" -ge 2 ] || die '--rows requires a value'
            rows="$2"
            shift 2
            ;;
        --width)
            [ "$#" -ge 2 ] || die '--width requires a value'
            width="$2"
            shift 2
            ;;
        --no-final-newline)
            no_final_newline=1
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

is_positive_int "$rows" || die '--rows must be a positive integer'
[ "$rows" -ge 2 ] || die '--rows must be at least 2'

if [ -n "$width" ]; then
    is_positive_int "$width" || die '--width must be a positive integer'
    cols="$width"
else
    cols="$(detect_width)"
fi
[ "$cols" -ge 2 ] || die 'terminal width must be at least 2 columns'

draw_border
for ((line = 0; line < rows - 2; line++)); do
    draw_inner
done

if [ "$no_final_newline" -eq 1 ]; then
    draw_border 0
else
    draw_border
fi
