#!/bin/sh
set -eu
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"   # repo root, resolved from this file
CAPS="--cap-drop ALL --cap-add SYS_ADMIN --cap-add SYS_PTRACE --cap-add NET_BIND_SERVICE \
--cap-add NET_RAW --cap-add SYS_CHROOT --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
--cap-add FSETID --cap-add KILL --cap-add SETGID --cap-add SETUID --cap-add SETPCAP \
--cap-add AUDIT_WRITE --cap-add SETFCAP --cap-add NET_ADMIN"
exec docker run --rm --init --network host $CAPS \
    --security-opt seccomp="$REPO/deploy/seccomp/sandlock-worker.json" \
    --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$REPO" \
    -e E2B_BASE_IMAGE=python:3.11-slim \
    -e E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    ${TRACE_ENV:-} \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$REPO:/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    timeout 200 python3 -m pytest "$@" -q -p no:cacheprovider
