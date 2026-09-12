#!/bin/sh

# Probe a minimal shell for binary-safe Base64 rescue options.

export LC_ALL=C
umask 077
PROBE_DIR=${TMPDIR:-/tmp}/standterm-base64d-probe-$$
if ! mkdir "$PROBE_DIR"; then
    echo 'base64d_probe.sh: cannot create a private temporary directory' >&2
    exit 1
fi
T=$PROBE_DIR/bytes.bin
trap 'rm -f "$T"; rmdir "$PROBE_DIR"' 0
trap 'exit 1' 1 2 3 15

have() {
    command -v "$1" >/dev/null 2>&1
}

same() {
    if [ "$VERIFY" = cksum ]; then
        # shellcheck disable=SC2046
        set -- $(cksum "$T" 2>/dev/null)
        [ "$1" = 1865030918 ] && [ "$2" = 3 ]
        return
    fi
    if [ "$VERIFY" = od ]; then
        H=$(od -An -tx1 "$T" 2>/dev/null) || H=
        # Word splitting turns the od byte list into positional parameters.
        # shellcheck disable=SC2086
        set -- $H
        [ "$#" = 3 ] && [ "$1" = 00 ] && [ "$2" = ff ] && [ "$3" = 0a ]
        return
    fi
    return 1
}

show() {
    if same; then
        echo "PASS  $1"
        return 0
    fi
    if have cksum; then
        R=$(cksum "$T" 2>/dev/null)
        echo "FAIL  $1 [$R]"
    else
        echo "FAIL  $1"
    fi
    return 1
}

echo 'Base64 rescue capability probe'
echo "shell: $0"
echo
echo 'Commands:'
for X in printf cksum od base64 openssl busybox awk sed gzip sha256sum; do
    if have "$X"; then echo "  yes  $X"; else echo "  no   $X"; fi
done

if have cksum; then
    VERIFY='cksum'
elif have od; then
    VERIFY='od'
else
    VERIFY=none
fi
echo
echo "Verifier: $VERIFY"

NATIVE=0
PRINTF_OK=0
ALT=0

echo
echo 'Native Base64 decoders:'
if have base64; then
    if echo AP8K | base64 -d >"$T" 2>/dev/null \
        || echo AP8K | base64 --decode >"$T" 2>/dev/null; then
        if show 'base64 decoder'; then NATIVE=1; fi
    else
        echo 'FAIL  base64 decoder [command error]'
    fi
else
    echo 'SKIP  base64 -d'
fi
if have busybox; then
    if echo AP8K | busybox base64 -d >"$T" 2>/dev/null; then
        if show 'busybox base64 -d'; then NATIVE=1; fi
    else
        echo 'FAIL  busybox base64 -d [command error]'
    fi
else
    echo 'SKIP  busybox base64 -d'
fi
if have openssl; then
    if echo AP8K | openssl base64 -d -A >"$T" 2>/dev/null \
        || echo AP8K | openssl enc -base64 -d -A >"$T" 2>/dev/null; then
        if show 'openssl Base64 decoder'; then NATIVE=1; fi
    else
        echo 'FAIL  openssl Base64 decoder [command error]'
    fi
else
    echo 'SKIP  openssl base64 -d -A'
fi

echo
echo 'Binary byte emitters:'
if have printf; then
    if printf '%b' '\0000\0377\0012' >"$T" 2>/dev/null; then
        if show 'printf %b with octal'; then PRINTF_OK=1; fi
    else
        echo 'FAIL  printf %b with octal [command error]'
    fi
else
    echo 'SKIP  printf %b with octal'
fi

# These intentionally probe non-portable echo escape behavior.
# shellcheck disable=SC2028
if echo '\0000\0377' >"$T" 2>/dev/null && show 'echo with octal'; then ALT=$((ALT + 1)); fi
# shellcheck disable=SC3037
if echo -e '\x00\xff' >"$T" 2>/dev/null && show 'echo -e with hex'; then ALT=$((ALT + 1)); fi

if have sed; then
    if echo AB | sed 's/A/\x00/;s/B/\xff/' >"$T" 2>/dev/null; then
        if show 'sed replacement with hex'; then ALT=$((ALT + 1)); fi
    else
        echo 'FAIL  sed replacement with hex [command error]'
    fi
else
    echo 'SKIP  sed replacement with hex'
fi

if have awk; then
    if awk 'BEGIN { printf "%c%c%c", 0, 255, 10 }' >"$T" 2>/dev/null; then
        if show 'awk printf with numeric bytes'; then ALT=$((ALT + 1)); fi
    else
        echo 'FAIL  awk printf with numeric bytes [command error]'
    fi
else
    echo 'SKIP  awk printf with numeric bytes'
fi

echo
if [ "$VERIFY" = none ]; then
    echo 'LEVEL ?: UNVERIFIED - cksum and compatible od are unavailable.'
elif [ "$NATIVE" -eq 1 ]; then
    echo 'LEVEL 3: READY - a native Base64 decoder passed.'
elif [ "$PRINTF_OK" -eq 1 ]; then
    echo 'LEVEL 2: READY - base64d.sh can emit all binary bytes.'
elif [ "$ALT" -gt 0 ]; then
    echo 'LEVEL 1: TARGET-SPECIFIC - an alternative byte emitter passed.'
else
    echo 'LEVEL 0: BLOCKED - no verified binary byte emitter is available.'
fi

if have gzip; then
    echo 'gzip: READY'
else
    echo 'gzip: UNAVAILABLE - transfer uncompressed data.'
fi

exit 0
