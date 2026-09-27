#!/bin/sh
# One security case (or a -k filter) in the production-shaped root worker, x86_64.
# Usage: x86-security-one.sh <E2B_BASE_IMAGE> <log> <pytest args...>
set -eu
cd "$(cd "$(dirname "$0")/../../.." && pwd)"   # repo root, resolved from this file
base="$1"; log="$2"; shift 2
SECCOMP_PROFILE="$(pwd)/deploy/seccomp/sandlock-worker.json"
docker run --rm --init --network host \
    --cap-drop ALL \
    --cap-add SYS_ADMIN --cap-add SYS_PTRACE --cap-add NET_BIND_SERVICE --cap-add NET_RAW \
    --cap-add SYS_CHROOT --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add FSETID \
    --cap-add KILL --cap-add SETGID --cap-add SETUID --cap-add SETPCAP --cap-add AUDIT_WRITE \
    --cap-add SETFCAP --cap-add NET_ADMIN \
    --security-opt seccomp="$SECCOMP_PROFILE" \
    --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$(pwd)" \
    -e E2B_BASE_IMAGE="$base" \
    -e E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    -e E2B_REAL_ROOT=0 \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    python -m pytest -q -p no:cacheprovider "$@" > "$log" 2>&1
