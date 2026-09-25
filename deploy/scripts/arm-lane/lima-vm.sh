#!/bin/sh
# The real-kernel aarch64 lane: a Lima qemu VM (Ubuntu 24.04 + a 6.14 kernel)
# that runs the cross-built aarch64 test binaries on an actual Linux kernel,
# with no containers and no qemu-user in between.
#
# Why the run root is *copied* into the guest rather than mounted:
#
#   Landlock's rulesets are accepted on a 9p mount but never match there. A
#   minimal reproducer -- create a ruleset granting EXECUTE|READ_FILE|READ_DIR
#   on a directory, landlock_restrict_self, then execv() a file inside it --
#   comes back EACCES when the directory is on the 9p mount and succeeds when
#   the same directory is on the guest's own disk. Every sandbox test that
#   confines a workspace path (fs_read/fs_write/fs_mount) is therefore refused
#   unless that path is a real filesystem inside the guest.
#
# So the host tree is exposed read-only as transport only:
#
#   /lima-repo                                              host repo, 9p, ro
#   /Users/polus/project/ai/sandlock-e2b/third_party/sandlock  mirrored source
#   /var/tmp/aarch64-target                                     mirrored artifacts
#   /src -> the mirrored source   (this is what CARGO_MANIFEST_DIR baked)
#
# Usage:
#   deploy/scripts/arm-lane/lima-vm.sh sync            mirror source + test artifacts in
#   deploy/scripts/arm-lane/lima-vm.sh run <path> ...  run a program (and args) in the guest
#   deploy/scripts/arm-lane/lima-vm.sh shell           interactive shell in the guest
#   deploy/scripts/arm-lane/lima-vm.sh start|stop      drive the VM
set -eu

name=sandlock-arm
# The repo root and the *guest* mirror path. The scripts live in
# deploy/scripts/arm-lane/, so the root is three levels up; the guest mirror
# keeps the host's absolute path by default purely so the two sides read the
# same strings in logs and `cd` args (override with ARM_LANE_GUEST_MIRROR).
repo="$(cd "$(dirname "$0")/../../.." && pwd)"
here="$(cd "$(dirname "$0")" && pwd)"
mirror="${ARM_LANE_GUEST_MIRROR:-$repo}"
debug_rel=aarch64-unknown-linux-gnu/debug

guest() {
    limactl shell "$name" -- bash -c "$1"
}

# The chroot tests exec tests/rootfs-helper, which build.rs compiles with the
# host `cc` -- an x86_64 binary. The lane needs the aarch64 one.
build_helper() {
    [ -f "$repo/tmp/arm-vm/rootfs-helper-aarch64" ] && return 0
    echo "building the aarch64 rootfs-helper"
    docker run --rm -v "$repo/third_party/sandlock":/src -v "$repo/tmp/arm-vm":/out \
        -e ZIG_TARGET=aarch64-linux-musl sandlock-zig-builder:local \
        zigcc -static -O2 -o /out/rootfs-helper-aarch64 tests/rootfs-helper.c
}

case "${1:-}" in
sync)
    shift
    build_helper
    # Transport: ssh, never the 9p mount. Lima exposes the host tree read-only
    # as /lima-repo, and that mount serves a *stale* file when the host rewrote
    # it in place: after `printf > f` on the host (6 -> 29 bytes), the guest
    # still read the 6-byte version with the old mtime, unchanged 12s later
    # (measured 2026-09-24). rsync's quick check therefore decides there is
    # nothing to copy and the lane silently runs the *previous* build -- the
    # first RED of this stage ran against a stale binary that way. So the
    # source tree travels as a tar stream and the artifacts go through
    # `limactl copy`; both are ssh. 9p stays only as a convenience view.
    guest "set -eu
        sudo mkdir -p '$mirror/third_party/sandlock' /var/tmp/aarch64-target/$debug_rel
        sudo chown -R \$(id -u):\$(id -g) '$mirror' /var/tmp/aarch64-target"
    # target-linux (53G), tmp (17G) and wheels are build output, not source;
    # the rest of the tree is ~15M.
    tar -C "$repo/third_party/sandlock" \
        --exclude 'target*/' --exclude 'tmp/' --exclude 'wheels/' --exclude '.git' \
        -cf - . | guest "tar -C '$mirror/third_party/sandlock' -xf -"
    # `sandlock` is the CLI the integration suite spawns through the
    # CARGO_BIN_EXE_sandlock path cargo baked in at compile time; without it
    # every test_control case dies with `spawn sandlock: NotFound`.
    #
    # The three root phases and the C-ABI suite travel for the same reason, and
    # as one tar stream rather than a `limactl copy` per file: they are ~4G of
    # test binaries, and the stream also sidesteps the guest sshd's refusal to
    # open over an existing destination ("dest open ... Failure", measured
    # 2026-09-24) that the per-file form had to work around.
    #
    # Which files: `cargo test -p X` emits one test binary per target (lib unit
    # tests, main unit tests, tests/*.rs), so a suite's baseline count is the
    # sum over all of them -- `scripts/test-all.sh` sums the same way. The
    # phases also exec the sibling binaries cargo baked in at compile time
    # (`CARGO_BIN_EXE_sandlock-supervise`, `..._sandlock-oci`, the CLI).
    #
    # Resolved through cargo's own dep-info rather than by target *name*: two
    # crates here have a `tests/integration.rs` (sandlock-core = 551 cases,
    # sandlock-oci = its own), and the rebuild suffix is a metadata hash that a
    # hardcoded list would silently drop. Newest matching .d wins, so a stale
    # duplicate from an earlier build never travels.
    #
    # `supervise_cost` is deliberately absent: it needs the --release binary
    # (`CARGO_BIN_EXE_sandlock-supervise` under --release) and measures PSS and
    # latency, which an emulated lane cannot judge.
    pick() {  # pick <target source file> -> newest binary built from it
        for d in $(ls -t "$repo/tmp/arm-lane/target/$debug_rel/deps/"*.d); do
            grep -q -- "$1" "$d" || continue
            [ -f "${d%.d}" ] || continue
            printf 'deps/%s\n' "$(basename "${d%.d}")"
            return 0
        done
        printf 'no aarch64 build of the target %s -- run xbuild.sh first\n' "$1" >&2
        exit 1
    }
    list="$repo/tmp/arm-lane/artifacts.txt"
    : > "$list"
    for target in \
        crates/sandlock-core/src/lib.rs crates/sandlock-core/tests/integration.rs \
        crates/sandlock-ffi/src/lib.rs \
        crates/sandlock-ffi/tests/c_smoke.rs crates/sandlock-ffi/tests/failure_reason.rs \
        crates/sandlock-ffi/tests/fs_mount.rs crates/sandlock-ffi/tests/handler_smoke.rs \
        crates/sandlock-ffi/tests/instance_exec.rs crates/sandlock-ffi/tests/policy_fn.rs \
        crates/sandlock-ffi/tests/popen.rs crates/sandlock-ffi/tests/protection.rs \
        crates/sandlock-ffi/tests/restore.rs \
        crates/sandlock-oci/src/lib.rs crates/sandlock-oci/src/main.rs \
        crates/sandlock-oci/tests/integration.rs crates/sandlock-oci/tests/test_init_reaper.rs \
        crates/sandlock-oci/tests/test_kill_all_delivery.rs \
        crates/sandlock-oci/tests/test_process_groups.rs \
        crates/sandlock-supervise/src/lib.rs crates/sandlock-supervise/src/main.rs \
        crates/sandlock-supervise/tests/supervise.rs \
        crates/sandlock-supervise/tests/supervise_root.rs \
        crates/sandlock-supervise/tests/mediation_2uid.rs; do
        picked="$(pick "$target")"
        printf '%s\n' "$picked" >> "$list"
        # The dep-info travels with it: the in-guest phase runner resolves
        # suites the same way (a target source -> its newest binary).
        [ -f "$repo/tmp/arm-lane/target/$debug_rel/$picked.d" ] \
            && printf '%s\n' "$picked.d" >> "$list"
        true
    done
    # `libsandlock_ffi.so` is what the C-ABI suite links against and what the
    # fork's ctypes binding loads; both resolve it through `<repo>/target`.
    printf '%s\n' sandlock sandlock-oci sandlock-supervise libsandlock_ffi.so >> "$list"
    ls -d "$repo/tmp/arm-lane/target/$debug_rel/"build/sandlock-core-*/out/restore-stub \
        | sed "s|^$repo/tmp/arm-lane/target/$debug_rel/||" >> "$list"
    tar -C "$repo/tmp/arm-lane/target/$debug_rel" -cf - -T "$list" \
        | guest "sudo tar -C /var/tmp/aarch64-target/$debug_rel -xf -"
    limactl copy --backend=scp "$repo/tmp/arm-vm/rootfs-helper-aarch64" \
        "$name:$mirror/third_party/sandlock/tests/rootfs-helper" >/dev/null
    guest "set -eu
        chmod +x '$mirror/third_party/sandlock/tests/rootfs-helper'
        # /src is a *bind mount*, not a symlink. CARGO_MANIFEST_DIR is baked as
        # /src/... and the root phases canonicalize their tmp root, so a
        # symlink here resolves back to the full <mirror>/... path and the
        # registered control socket then blows the 108-byte SUN_LEN budget
        # (measured 2026-09-24: supervise_root came back with \"path must be
        # shorter than SUN_LEN\" while the x86_64 container, whose repo sits at
        # /workspace, stayed green).
        if ! mountpoint -q /src; then
            sudo rm -f /src
            sudo mkdir -p /src
            sudo mount --bind '$mirror/third_party/sandlock' /src
        fi
        echo 'synced'
        ls -l /src/tests/rootfs-helper
        ls -l /var/tmp/aarch64-target/$debug_rel/deps/sandlock_core-*
        ls /var/tmp/aarch64-target/$debug_rel/deps/ | grep -E \
            '^(sandlock_core|integration|sandlock_ffi|sandlock_oci|supervise|supervise_root|mediation_2uid)-' | head"
    sh "$here/lima-vm.sh" prep
    ;;
prep)
    # Root prep the test container's entrypoint performs before dropping to an
    # unprivileged uid (the sandlock-dev image's docker-entrypoint.sh): the
    # wildcard/egress fixtures must be pre-seeded as root so `net_fixture.rs`
    # takes its unprivileged path. Streamed over stdin, not 9p (see sync).
    limactl shell "$name" -- bash -s < "$here/guest-prep.sh"
    ;;
run)
    shift
    # The guest inherits the host's `http_proxy`/`https_proxy` (Lima forwards
    # them), and the workloads under test honour them: the ACL/egress fixtures
    # exec a python that then dials the *host* proxy instead of the address
    # under test, which the sandbox denies. `test_http_acl::test_http_allow_get`
    # failed with "urlopen error [Errno 111] Connection refused" and strace
    # showed the one and only connect() of the whole run going to the proxy
    # (measured 2026-09-24). The container lane has no proxy env at all, so
    # clear it here.
    guest "unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY; $*"
    ;;
shell)
    limactl shell "$name"
    ;;
start)
    limactl start "$name" --tty=false
    ;;
stop)
    limactl stop "$name"
    ;;
*)
    sed -n '2,30p' "$0"
    exit 2
    ;;
esac
