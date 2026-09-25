#!/bin/sh
# Mirror the E2B side of the repo into the arm64 guest, so `pytest tests/security`
# can run there: the host is x86_64 macOS, and the security suite needs a real
# Linux kernel with Landlock, a root-capable worker shape and the aarch64
# sandlock binding.
#
# Transport is tar over ssh, never the 9p mount -- see the stale-file trap in
# deploy/scripts/arm-lane/lima-vm.sh's sync case.
#
# What travels:
#   * the python packages the suite imports (control_plane, envd_service,
#     gateway_common) plus the test tree's shared pieces (conftest,
#     _disk_projids); tests/sdk and tests/contract are 58M of suite this lane
#     does not run, and `tests/perf` is not collected here either
#   * the fork's ctypes binding (python/src/sandlock) and the cross-built
#     aarch64 libsandlock_ffi.so, dropped side by side so _find_lib() step 2
#     finds it (the binding is ctypes, so no CPython ABI/wheel is involved)
#
# Usage: deploy/scripts/arm-lane/e2b-sync.sh
set -eu
name=sandlock-arm
# The repo root and the *guest* mirror path. The scripts live in
# deploy/scripts/arm-lane/, so the root is three levels up; the guest mirror
# keeps the host's absolute path by default purely so the two sides read the
# same strings in logs and `cd` args (override with ARM_LANE_GUEST_MIRROR).
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
mirror="${ARM_LANE_GUEST_MIRROR:-$repo}"
venv=/opt/e2b-venv

guest() { limactl shell "$name" -- bash -c "$1"; }

guest "set -eu
    sudo mkdir -p '$mirror' $venv/lib/python3.12/site-packages
    sudo chown -R \$(id -u):\$(id -g) '$mirror'"

# 1. the E2B python tree (no third_party: that mirror is the fork's own sync)
tar -C "$repo" \
    --exclude '__pycache__' --exclude '.git' \
    -cf - pyproject.toml control_plane envd_service gateway_common \
    tests/__init__.py tests/conftest.py tests/_disk_projids.py tests/security \
    | guest "tar -C '$mirror' -xf -"

# The real-root gate's unit file travels too, on its own: it holds the pin that
# `_REAL_ROOT_PROBE` asks for the pivot_root *this* architecture has. That pin
# only bites on the generic-syscall-table arches, so this lane is the only place
# it can be checked (measured 2026-09-24: the probe hardcoded x86_64's 155, which
# on aarch64 is `sched_getattr` -- the gate was refused with ESRCH and reported
# as a seccomp problem, so E2B_REAL_ROOT could never be armed here).
tar -C "$repo" -cf - tests/unit/__init__.py tests/unit/test_real_root_gate.py \
    | guest "tar -C '$mirror' -xf -"

# 2. the sandlock binding + the aarch64 .so next to it
so="$repo/tmp/arm-lane/target/aarch64-unknown-linux-gnu/debug/libsandlock_ffi.so"
[ -f "$so" ] || { echo "missing $so -- build it first (xbuild.sh cargo build -p sandlock-ffi)" >&2; exit 1; }
tar -C "$repo/third_party/sandlock/python/src" --exclude '__pycache__' -cf - sandlock \
    | guest "sudo tar -C $venv/lib/python3.12/site-packages -xf -"
limactl copy --backend=scp "$so" "$name:/tmp/libsandlock_ffi.so" >/dev/null

# 3. the mediator binary route B spawns per sandbox. envd_service/route_b.py
#    looks for it as `<sandlock package>/bin/sandlock-supervise`, i.e. the
#    wheel's layout -- without it every mediated (chroot) test dies with
#    "the installed sandlock wheel has no sandlock-supervise".
sv="$repo/tmp/arm-lane/target/aarch64-unknown-linux-gnu/debug/sandlock-supervise"
[ -f "$sv" ] || { echo "missing $sv -- build it first (xbuild.sh cargo build -p sandlock-supervise)" >&2; exit 1; }
limactl copy --backend=scp "$sv" "$name:/tmp/sandlock-supervise" >/dev/null
guest "set -eu
    sudo mv /tmp/libsandlock_ffi.so $venv/lib/python3.12/site-packages/sandlock/libsandlock_ffi.so
    sudo install -Dm755 /tmp/sandlock-supervise $venv/lib/python3.12/site-packages/sandlock/bin/sandlock-supervise
    sudo chown -R root:root $venv/lib/python3.12/site-packages/sandlock
    $venv/bin/python -c 'import sandlock, sys; print(\"sandlock\", sandlock.landlock_abi_version(), sys.version.split()[0])'"
echo "e2b-sync: ok"
