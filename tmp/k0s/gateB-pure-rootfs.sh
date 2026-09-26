#!/bin/sh
# gate B's twin: the *pure* shape with a synthesized root (E2B_PURE_ROOTFS=synth).
#
# `gateB-full.sh` is the same lane with `E2B_BASE_IMAGE=""` and no shape switch,
# i.e. N15's pure shape (mediator root = the host root, identity translation).
# This one flips the N16 switch so the sandbox pivots into a plain directory of
# its own -- and lets the caller pick `E2B_REAL_ROOT` (0 = emulated root, 1 =
# the fork's mount namespace + pivot_root), because the census needs both.
#
# The two extra env vars matter for a *different* reason than they look:
# `E2B_PURE_ROOTFS` is what `tests/security/conftest.py::route_b_sandbox` -- the
# security suite's only shape entry point (Task 5b, de0a817) -- mirrors into the
# executor. A lane that runs the suite without it measures the old N15 shape and
# passes vacuously. `E2B_PURE_ROOTFS_DIR` is deliberately left unset so the
# helper's own `sandbox_tmpdir(suffix="-pure-rootfs")` default applies (a
# directory the sandbox uid can walk into); set it to pin one, as the deployment
# does with `<workspace_base>/_pure_rootfs`.
#
# Usage: gateB-pure-rootfs.sh <E2B_REAL_ROOT 0|1> <log> [pytest target...]
# Default target is `tests` (the whole suite); the target is handed to pytest
# unquoted on purpose so several targets/selectors can be passed.
#
# `E2B_TEST_IMAGE` overrides the runner image (default
# `e2b-sandlock-test:latest`). It exists because the image bakes in
# `wheels/fork/*.whl` and nothing rebuilds it when those wheels change
# (`deploy/scripts/build-test-image.sh`, pitfalls §B7): re-running a lane on a
# re-baked image without touching the shared `:latest` tag needs a knob.
set -eu
cd /Users/polus/project/ai/sandlock-e2b
IMAGE="${E2B_TEST_IMAGE:-e2b-sandlock-test:latest}"
real_root="$1"
log="$2"
shift 2
target="${*:-tests}"
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
    -e E2B_REAL_ROOT="$real_root" \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    "$IMAGE" \
    python -m pytest $target -q -p no:cacheprovider > "$log" 2>&1
