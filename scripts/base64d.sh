#!/bin/sh
# POSIX sh Base64 decoder.
set -f
export LC_ALL=C

u() {
    printf '%s\n' 'Usage: base64d.sh <base64-string>' >&2
    exit 2
}
e() {
    printf 'base64d.sh: %s\n' "$1" >&2
    exit 1
}

A=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/
v() {
    case $A in
        *"$1"*) P=${A%%"$1"*}; V=${#P} ;;
        *) return 1 ;;
    esac
}
c() {
    v "$q1" || e 'invalid character'; x=$V
    v "$q2" || e 'invalid character'; y=$V
    if [ "$q3" = = ]; then
        [ "$q4" = = ] || e 'invalid padding'
        [ $((y % 16)) -eq 0 ] || e 'non-canonical padding'
        F=1; return
    fi
    v "$q3" || e 'invalid character'; z=$V
    if [ "$q4" = = ]; then
        [ $((z % 4)) -eq 0 ] || e 'non-canonical padding'
        F=1; return
    fi
    v "$q4" || e 'invalid character'
}
o() {
    B=$1
    O="${O}\\0$((B / 64))$(((B / 8) % 8))$((B % 8))"
    N=$((N + 1))
    if [ "$N" -ge 768 ]; then
        printf '%b' "$O" || e 'write failed'
        O=; N=0
    fi
}
d() {
    v "$q1"; x=$V
    v "$q2"; y=$V
    o $((x * 4 + y / 16))
    [ "$q3" = = ] && return
    v "$q3"; z=$V
    o $(((y % 16) * 16 + z / 4))
    [ "$q4" = = ] && return
    v "$q4"
    o $(((z % 4) * 64 + V))
}
s() {
    M=$1; Q=0
    while IFS= read -r L; do
        [ "${#L}" -le 4096 ] || e 'line exceeds 4096 characters'
        I=$L
        while [ -n "$I" ]; do
            R=${I#?}; C=${I%"$R"}; I=$R
            case $C in [[:space:]]) continue ;; esac
            if [ "$M" = c ]; then
                [ "$F" -eq 0 ] || e 'data follows padding'
            fi
            Q=$((Q + 1))
            case $Q in
                1) q1=$C ;; 2) q2=$C ;; 3) q3=$C ;;
                4)
                    q4=$C
                    if [ "$M" = c ]; then c; else d; fi
                    Q=0
                    ;;
            esac
        done
    done <<B64D_INPUT
$S
B64D_INPUT
    [ "$Q" -eq 0 ] || e 'incomplete quartet'
}

[ "$#" -eq 1 ] || u
S=$1; F=0
s c
O=; N=0
s d
[ "$N" -eq 0 ] || printf '%b' "$O" || e 'write failed'
exit 0
