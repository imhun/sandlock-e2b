#!/usr/bin/env bash
# Which syscall does this workload actually use: splice / vmsplice / sendfile /
# copy_file_range? Counts, no policy -- run it before arguing about blocking any
# of them (see docs/security-architecture.md, "能力边界" -> CVE-2026-53362: the
# user-space entry to that bug is splice(2), and the question "would a gate hurt
# the fleet?" is answered by a count, not by reading tool source).
#
# Usage:
#   probe_splice_usage.sh IMAGE -- CMD [ARGS...]
#   probe_splice_usage.sh python:3.14-slim -- python3 -c 'import os; ...'
#
# Runs CMD inside IMAGE under strace -f and prints the four counts. Needs Docker;
# installs strace in the container if it is missing (apt or apk). The host needs
# network only for that install. The payload is passed as argv (never re-quoted
# through a shell), so multi-line commands survive intact.
set -euo pipefail

IMAGE="${1:?usage: probe_splice_usage.sh IMAGE -- CMD [ARGS...]}"
shift
[ "${1:-}" = "--" ] && shift
[ "$#" -gt 0 ] || { echo "缺少要测的命令（-- 之后）" >&2; exit 2; }

# The payload arrives as "$@" (docker passes it as argv), so nothing is re-quoted.
read -r -d '' SCRIPT <<'SH' || true
set -e
if ! command -v strace >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    if command -v apt-get >/dev/null; then
        apt-get update -qq >/dev/null 2>&1
        apt-get install -y -qq -o Dpkg::Use-Pty=0 strace >/dev/null 2>&1
    elif command -v apk >/dev/null; then
        apk add --no-cache strace >/dev/null 2>&1
    else
        echo "no strace and no package manager" >&2
        exit 3
    fi
fi
: > /tmp/splice-trace.txt
rc=0
strace -f -qq -o /tmp/splice-trace.txt -e trace=splice,vmsplice,sendfile,copy_file_range "$@" >/dev/null 2>&1 || rc=$?
printf 'rc=%s splice=%s vmsplice=%s sendfile=%s copy_file_range=%s\n' \
    "$rc" \
    "$(grep -cE '(^| )splice\(' /tmp/splice-trace.txt || true)" \
    "$(grep -cE '(^| )vmsplice\(' /tmp/splice-trace.txt || true)" \
    "$(grep -cE '(^| )sendfile\(' /tmp/splice-trace.txt || true)" \
    "$(grep -cE '(^| )copy_file_range\(' /tmp/splice-trace.txt || true)"
SH

docker run --rm --cap-add=SYS_PTRACE --entrypoint sh "$IMAGE" -c "$SCRIPT" probe "$@"
