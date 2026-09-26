#!/bin/sh
# gate B's twin: the *pure* shape (`E2B_BASE_IMAGE=""`) in both root states.
#
# `gateB-full.sh` is the same lane with no shape switch at all: N15's pure shape
# (mediator root = the host root, identity translation). This one takes the
# state as its first argument, and the two states are the ones the 2026-09-26
# ruling leaves standing
# (`docs/superpowers/plans/2026-09-26-decisions.md`, "「合成根 + 模拟根」结构性不成立"):
#
#   state 0 -- N15 identity: no synthesized root (`E2B_PURE_ROOTFS` unset,
#              `E2B_REAL_ROOT=0`).
#   state 1 -- synthesized root + real root (`E2B_PURE_ROOTFS=synth`,
#              `E2B_REAL_ROOT=1`).
#
# The third combination -- `synth` with `E2B_REAL_ROOT=0` -- is structurally
# impossible and is deliberately *not* expressible here. The synthesized root is
# an empty skeleton, and only the real-root path (the fork's mount namespace +
# pivot_root) performs the binds, so such a sandbox dies in the generated
# container's `execvp("/bin/sh")` (EACCES) and every later verb answers
# `InstanceClosed` (Task 9's 32-error log: `tmp/k0s/task9/`, and the deployment
# settings pair is refused outright since d1c4922). A lane asking for that pair
# would measure a configuration no deployment may have, not a shape.
#
# `E2B_PURE_ROOTFS` matters for a *different* reason than it looks:
# it is what `tests/security/conftest.py::route_b_sandbox` -- the security
# suite's only shape entry point (Task 5b, de0a817) -- mirrors into the
# executor. A lane that runs the suite without it measures the identity shape.
# `E2B_PURE_ROOTFS_DIR` is deliberately left unset so the helper's own
# `sandbox_tmpdir(suffix="-pure-rootfs")` default applies (a directory the
# sandbox uid can walk into); set it to pin one, as the deployment does with
# `<workspace_base>/_pure_rootfs`.
#
# Usage: gateB-pure-rootfs.sh <state 0|1> <log> [pytest target...]
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
state="$1"
log="$2"
shift 2
target="${*:-tests}"
SECCOMP_PROFILE="$(pwd)/deploy/seccomp/sandlock-worker.json"

# The state decides *both* switches (see the header): `set --` keeps the two
# `-e` pairs as separate argv entries, one `if` per state so `set -e` is not
# asked to interpret a failed test as an error.
case "$state" in
    0|1) ;;
    *) echo "usage: $0 <state 0|1> <log> [pytest target...]" >&2; exit 2 ;;
esac
set -- -e E2B_REAL_ROOT=0
if [ "$state" = "1" ]; then
    set -- -e E2B_PURE_ROOTFS=synth -e E2B_REAL_ROOT=1
fi

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
    -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    -e E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    "$@" \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    "$IMAGE" \
    python -m pytest $target -q -p no:cacheprovider > "$log" 2>&1
