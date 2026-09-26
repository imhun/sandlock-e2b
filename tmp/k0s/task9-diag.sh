#!/bin/sh
# Task 9 diagnostics: run one probe inside the same container shape as
# `gateB-pure-rootfs.sh` (worker caps + worker seccomp profile), so a shape's
# behaviour is measured on the deployment's own privilege surface.
#
# Unlike the lane, this one hands the sandbox child a writable
# `SANLOCK_REALROOT_TRACE` file: the slot shapes discard the child's stderr, so
# that file is the only place a launch-time failure says *why* it died.
#
# Usage: task9-diag.sh <probe.py> <shape> <log>
set -eu
cd /Users/polus/project/ai/sandlock-e2b
probe="$1"
shape="$2"
log="$3"
TRACE="tmp/k0s/task9/trace-$shape.txt"
: > "$TRACE"
chmod 666 "$TRACE"
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
    -e E2B_TEST_STRICT_SKIPS=1 \
    -e E2B_BASE_IMAGE= \
    -e E2B_PURE_ROOTFS=synth \
    -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    -e E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    -e E2B_REAL_ROOT=0 \
    -e SANLOCK_REALROOT_TRACE="/workspace/$TRACE" \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    e2b-sandlock-test:latest \
    python "$probe" "$shape" > "$log" 2>&1
echo "==> $TRACE"
cat "$TRACE"
