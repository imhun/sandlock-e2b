#!/bin/sh
# N27 Task 7: run a command inside the prod-shaped lane container.
#
# Same cap set / shipped seccomp profile / registry mirror as
# `deploy/scripts/arm-lane/x86-security.sh`, with two deliberate differences:
#
#   * `E2B_BASE_IMAGE` is passed *empty* (the pure shape). `${VAR:-default}`
#     would silently substitute the default here, so the value is written out.
#   * the repo is mounted at `/src`, not `/workspace`. In the pure (identity)
#     shape the mediator refuses `/workspace`, so a fixture under a repo mounted
#     there cannot even be chdir'd into (exit 125) -- see
#     tmp/k0s/n27-t7-lane-mount.log.
#
# `E2B_PURE_ROOTFS` is *forwarded* rather than pinned, because since 2026-09-27
# the product's default is the synthesized root: the probe's own `--shape`
# argument has to match the shape the sandbox actually runs, and
# `E2B_PURE_ROOTFS=off` is the one key back to the identity root this lane was
# written against. Unset/empty forwards as the default (synthesized).
#
# Usage: sh deploy/scripts/acceptance/n27-t7-lane.sh python3 -u deploy/scripts/acceptance/probe_state_base_visibility.py lane --shape identity --layout n27
set -eu
# Three levels: same promotion bug as `gateA-full.sh` -- from
# `deploy/scripts/acceptance/`, `../..` lands on `deploy/`, which would mount
# `deploy/` as this lane's `/src`.
REPO="$(cd "$(dirname "$0")/../../.." && pwd)"
IMAGE="${IMAGE:-e2b-sandlock-test:latest}"
SECCOMP="${SECCOMP_PROFILE:-$REPO/deploy/seccomp/sandlock-worker.json}"

exec docker run --rm --init --network host \
    --cap-drop ALL \
    --cap-add SYS_ADMIN --cap-add SYS_PTRACE --cap-add NET_BIND_SERVICE --cap-add NET_RAW \
    --cap-add SYS_CHROOT --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER --cap-add FSETID \
    --cap-add KILL --cap-add SETGID --cap-add SETUID --cap-add SETPCAP --cap-add AUDIT_WRITE \
    --cap-add SETFCAP --cap-add NET_ADMIN \
    --security-opt seccomp="$SECCOMP" \
    --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT=/src \
    -e E2B_BASE_IMAGE= \
    -e E2B_PURE_ROOTFS \
    -e E2B_REGISTRY_MIRRORS="${E2B_REGISTRY_MIRRORS:-registry-1.docker.io=127.0.0.1:5080}" \
    -e PYTHONPATH=/src \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$REPO:/src" -w /src \
    "$IMAGE" "$@"
