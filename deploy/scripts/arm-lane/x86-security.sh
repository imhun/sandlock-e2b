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
# Usage: deploy/scripts/arm-lane/x86-security.sh <E2B_REAL_ROOT 0|1> <log path>
set -eu
cd "$(dirname "$0")/../../.."

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
