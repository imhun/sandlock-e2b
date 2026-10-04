#!/bin/sh
# tests/security alone, in the production-shaped root worker, on x86_64.
#
# `deploy/scripts/test-prod-shaped.sh` runs `pytest tests` (the whole tree) and
# appends the caller's args, so it cannot narrow to one directory (pitfalls B4).
# This replicates its phase-1 invocation -- same image, same cap set, same
# shipped seccomp profile, same registry mirror -- with `tests/security` as the
# target, so the arm64 lane's two-state result has an x86_64 counterpart taken
# the same way.
#
# Usage: deploy/scripts/arm-lane/x86-security.sh 1 <log path>
#
# **N14 S5 (2026-10-04)**: the `0` arm is retired. `E2B_REAL_ROOT=0` (the
# emulated root) and `E2B_PURE_ROOTFS=off` (N15's identity root) are refused by
# name at startup now, so there is one shape to run -- and a run book that still
# says `0` has to be told so at the boundary, not handed a shape nothing tests.
set -eu
cd "$(dirname "$0")/../../.."

if [ "${1:-}" = "0" ]; then
    echo "x86-security: E2B_REAL_ROOT=0 is retired (N14 S5): the real root is the only shape now, so there is no 0 arm to run -- pass 1" >&2
    exit 2
fi
if [ "${1:-}" != "1" ]; then
    echo "usage: deploy/scripts/arm-lane/x86-security.sh 1 <log path>" >&2
    exit 2
fi

real_root="$1"
log="$2"
SECCOMP_PROFILE="$(pwd)/deploy/seccomp/sandlock-worker.json"

set -- --cap-drop ALL
for cap in SYS_ADMIN SYS_PTRACE NET_BIND_SERVICE NET_RAW SYS_CHROOT CHOWN DAC_OVERRIDE \
           FOWNER FSETID KILL SETGID SETUID SETPCAP AUDIT_WRITE SETFCAP NET_ADMIN; do
    set -- "$@" --cap-add "$cap"
done

docker run --rm --init --network host "$@" \
    --security-opt seccomp="$SECCOMP_PROFILE" \
    --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$(pwd)" \
    -e E2B_BASE_IMAGE=python-mcp:3.14 \
    -e E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    -e E2B_REAL_ROOT="$real_root" \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    python -m pytest tests/security -q -p no:cacheprovider > "$log" 2>&1
