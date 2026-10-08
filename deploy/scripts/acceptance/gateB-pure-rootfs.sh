#!/bin/sh
# gate B's twin: the *pure* shape (`E2B_BASE_IMAGE=""`), one root state.
#
# **N14 S5 (2026-10-04)**: there were two states, and the other one is retired.
# `E2B_PURE_ROOTFS=off` (N15's identity root) and `E2B_REAL_ROOT=0` (the
# emulated root) are both refused by name at startup now
# (`refuse_retired_root_levers`), which leaves the pure shape exactly one root:
# the synthesized skeleton (N16) plus the real root that binds it. So this lane
# takes no state argument any more -- a run book that still passes `0` is told
# so at the boundary rather than handed a shape nothing tests.
#
# The state that used to be expressible and is *not* any more:
#
#   * `synth` with an emulated root was structurally impossible to begin with
#     (`docs/superpowers/plans/2026-09-26-decisions.md`, "「合成根 + 模拟根」结构性
#     不成立"): the synthesized root is an empty skeleton, and only the
#     real-root path (the fork's mount namespace + pivot_root) performs the
#     binds, so such a sandbox dies in the generated container's
#     `execvp("/bin/sh")` (EACCES) and every later verb answers
#     `InstanceClosed` (Task 9's 32-error log: `tmp/k0s/task9/`; the pair is
#     refused outright since d1c4922 and by name since N14 S5).
#
# `E2B_PURE_ROOTFS=synth` is still named below even though it is the product's
# default (2026-09-27 ruling): the lane's subject is the shape, so it says the
# shape instead of inheriting it.
#
# `E2B_PURE_ROOTFS_DIR` is deliberately left unset so the helper's own
# `sandbox_tmpdir(suffix="-pure-rootfs")` default applies (a directory the
# sandbox uid can walk into); set it to pin one, as the deployment does with
# `<workspace_base>/_pure_rootfs`. `tests/security/conftest.py::own_identity_sandbox`
# -- the security suite's only shape entry point (Task 5b, de0a817) -- reads
# the *directory* knob and synthesizes the root unconditionally since N14 S5.
#
# Usage: gateB-pure-rootfs.sh 1 <log> [pytest target...]
# Default target is `tests` (the whole suite); the target is handed to pytest
# unquoted on purpose so several targets/selectors can be passed.
#
# `E2B_TEST_IMAGE` overrides the runner image (default
# `e2b-sandlock-test:latest`). It exists because the image bakes in
# `wheels/fork/*.whl` and nothing rebuilds it when those wheels change
# (`deploy/scripts/build-test-image.sh`, pitfalls §B7): re-running a lane on a
# re-baked image without touching the shared `:latest` tag needs a knob.
set -eu
# Repo root, three levels (same promotion bug as `gateA-full.sh`/`gateB-full.sh`:
# from `deploy/scripts/acceptance/`, `../..` lands on `deploy/`).
cd "$(cd "$(dirname "$0")/../../.." && pwd)"
IMAGE="${E2B_TEST_IMAGE:-e2b-sandlock-test:latest}"
state="$1"
log="$2"
shift 2
target="${*:-tests}"
SECCOMP_PROFILE="$(pwd)/deploy/seccomp/sandlock-worker.json"

# The one shape (see the header). The retired state is refused by name at the
# boundary: a run book that still says `0` has to be told, not handed a shape
# nothing tests.
case "${state:-}" in
    1) ;;
    0)
        echo "gateB-pure-rootfs: state 0 is retired (N14 S5): the identity pure root (E2B_PURE_ROOTFS=off + E2B_REAL_ROOT=0) is refused by name at startup now, so there is no 0 state to run -- pass 1" >&2
        exit 2
        ;;
    *) echo "usage: $0 1 <log> [pytest target...]" >&2; exit 2 ;;
esac
set -- -e E2B_PURE_ROOTFS=synth

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
