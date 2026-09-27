#!/bin/sh
# Run one command in the prod-shaped lane (the same container shape as
# deploy/scripts/test-prod-shaped.sh phase 1), for the N35 probes.
#
#   sh tmp/k0s/n35-lane.sh python3 -u tmp/k0s/probe_n35_exec_gate.py chroot all
#
# SANLOCK_EVENT_TRACE=1 is on: the fork's step trace goes to the container's
# stderr, which is exactly where this script leaves it.
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
IMAGE="${IMAGE:-e2b-sandlock-test:latest}"
SECCOMP="${SECCOMP_PROFILE:-$REPO/deploy/seccomp/sandlock-worker.json}"

# PROD_DROP_CAPS=SYS_ADMIN[,SYS_PTRACE] narrows the lane to the pinned
# production shape, the same way deploy/scripts/test-prod-shaped.sh does: the
# cap has to leave the --cap-add loop, because this engine resolves --cap-add
# over --cap-drop regardless of order.
ALL_CAPS="SYS_ADMIN SYS_PTRACE NET_BIND_SERVICE NET_RAW SYS_CHROOT CHOWN \
DAC_OVERRIDE FOWNER FSETID KILL SETGID SETUID SETPCAP AUDIT_WRITE SETFCAP NET_ADMIN"
DROPPED="$(printf '%s' "${PROD_DROP_CAPS:-}" | tr ',' ' ')"
CAPS="--cap-drop ALL"
for cap in $ALL_CAPS; do
    case " $DROPPED " in
        *" $cap "*) CAPS="$CAPS --cap-drop $cap" ;;
        *) CAPS="$CAPS --cap-add $cap" ;;
    esac
done

exec docker run --rm --init --network host \
    $CAPS \
    ${LANE_USER:+--user $LANE_USER} \
    --security-opt seccomp="$SECCOMP" \
    --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$REPO" \
    -e E2B_BASE_IMAGE="${E2B_BASE_IMAGE:-python:3.11-slim}" \
    -e E2B_REGISTRY_MIRRORS="${E2B_REGISTRY_MIRRORS:-registry-1.docker.io=127.0.0.1:5080}" \
    -e SANLOCK_EVENT_TRACE="${SANLOCK_EVENT_TRACE:-1}" \
    -e PYTHONPATH=/workspace \
    -e E2B_REAL_ROOT="${E2B_REAL_ROOT:-}" \
    -e SANLOCK_REALROOT_TRACE="${SANLOCK_REALROOT_TRACE:-/workspace/tmp/n35-realroot-error.txt}" \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$REPO:/workspace" -w /workspace \
    "$IMAGE" "$@"
