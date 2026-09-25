#!/bin/sh
# The fork's root phases plus the C-ABI suite, on the aarch64 lane (guest side).
#
# `scripts/test-all.sh` drives these with cargo from a checkout; the guest has
# no toolchain -- the binaries are cross-built on the host (`xbuild.sh`) and
# copied in -- so this runs the same test binaries directly and prints the same
# per-suite sum the gate compares against `docs/test-baseline.md`:
#
#   ffi            cargo test -p sandlock-ffi
#   oci            cargo test -p sandlock-oci                              (root)
#   supervise      cargo test -p sandlock-supervise --lib --test supervise
#   supervise_root cargo test -p sandlock-supervise --test supervise_root  (root)
#   mediation_2uid cargo test -p sandlock-supervise --test mediation_2uid  (root)
#
# Run from the host: limactl shell sandlock-arm -- bash -s < this-file
set -eu
mirror="${ARM_LANE_GUEST_MIRROR:-/Users/polus/project/ai/sandlock-e2b}"
# ^ the guest's own copy of the repo (this file runs *inside* the guest,
#   streamed over stdin, so the host cannot hand it anything).
rel=/var/tmp/aarch64-target/aarch64-unknown-linux-gnu/debug
src=/src

# The root phases spawn their peers as foreign uids (65533 / 65534) and the
# fixtures keep their data under <repo>/tmp and the cargo target tmp, exactly
# as `scripts/test-all.sh` prepares for its root phases.
sudo mkdir -p "$src/tmp" "$rel/tmp"
sudo chmod -R a+rwX "$src/tmp" "$rel/tmp"

# Two lanes' worth of shape, no product behaviour:
#
# * The root phases find the CLI and the cdylib through the paths their build
#   baked in (`<manifest>/../../target/debug/sandlock`, `<repo>/target/debug/
#   libsandlock_ffi.so`). On this lane `target` is the cross-build output, so
#   point it there instead of leaving it dangling at the container's path.
# * The C-ABI suite shells out to `cargo build -p sandlock-ffi --lib` so it
#   links a *current* cdylib rather than a stale one. There is no toolchain in
#   the guest, so the shim below stands in for that rebuild by refusing to
#   pass unless the host's freshly cross-built artifact is newer than every
#   source it was built from -- the same "not stale" guarantee, checked
#   against the artifact the host just produced.
triple=aarch64-unknown-linux-gnu
sudo ln -sfn "/var/tmp/aarch64-target/$triple" /src/target-linux

# Same reason as in lima-vm.sh's sync: /src must be a bind mount, or the root
# phases canonicalize their tmp root into the long <mirror>/... path and the
# registered control socket exceeds SUN_LEN. Idempotent, so a re-run after a
# VM restart re-establishes it.
if ! mountpoint -q /src; then
    sudo rm -f /src
    sudo mkdir -p /src
    sudo mount --bind "$mirror/third_party/sandlock" /src
fi

cat > /tmp/sandlock-lane-cargo <<'SHIM'
#!/bin/sh
set -eu
: "${CARGO_TARGET_DIR:?lane cargo shim needs CARGO_TARGET_DIR}"
so="$CARGO_TARGET_DIR/debug/libsandlock_ffi.so"
if [ ! -f "$so" ]; then
    echo "lane-cargo: $so is missing -- run xbuild.sh, then lima-vm.sh sync" >&2
    exit 1
fi
newer=""
for d in /src/crates/sandlock-ffi/src /src/crates/sandlock-ffi/include \
         /src/crates/sandlock-core/src; do
    [ -e "$d" ] || continue
    found=$(find "$d" -newer "$so" \( -name '*.rs' -o -name '*.h' \) -print -quit)
    newer="${newer}${found}"
done
if [ -n "$newer" ]; then
    echo "lane-cargo: $newer is newer than $so -- the cdylib is stale," >&2
    echo "lane-cargo: rebuild with xbuild.sh and re-sync" >&2
    exit 1
fi
SHIM
sudo install -m 0755 /tmp/sandlock-lane-cargo /usr/local/bin/sandlock-lane-cargo
rm -f /tmp/sandlock-lane-cargo

bin_for() {  # bin_for <target source file> -> the binary built from it
    for d in $(ls -t "$rel"/deps/*.d); do
        grep -q -- "$1" "$d" || continue
        [ -f "${d%.d}" ] || continue
        printf '%s\n' "${d%.d}"
        return 0
    done
    printf 'no binary for the target %s -- sync first\n' "$1" >&2
    exit 1
}

sum() {  # sum <log> -> total of the per-binary "test result: ok. N passed"
    grep -o 'test result: ok\. [0-9][0-9]* passed' "$1" | awk '{s+=$4} END {print s+0}'
}

suite() {  # suite <label> <uid501|root> <cwd> <target...>
    label="$1"; shift
    mode="$1"; shift
    cwd="$1"; shift
    log="/var/tmp/aarch64-target/lane-$label.log"
    : > "$log"
    printf '==> %s (%s, cwd %s)\n' "$label" "$mode" "$cwd"
    for target in "$@"; do
        bin="$(bin_for "$target")"
        printf '    %s\n' "$(basename "$bin")"
        if [ "$mode" = root ]; then
            ( cd "$cwd" && sudo "$bin" --test-threads=1 ) >>"$log" 2>&1 || true
        else
            ( cd "$cwd" && CARGO_TARGET_DIR="/var/tmp/aarch64-target/$triple" \
                SANLOCK_CARGO=/usr/local/bin/sandlock-lane-cargo \
                "$bin" --test-threads=1 ) >>"$log" 2>&1 || true
        fi
    done
    printf '    %s: %s passed\n' "$label" "$(sum "$log")"
    grep -E '^test .* FAILED|^failures:|^error' "$log" | head -20 || true
}

ffi=crates/sandlock-ffi
oci=crates/sandlock-oci
sup=crates/sandlock-supervise

suite ffi uid501 "$src/$ffi" \
    "$ffi/src/lib.rs" "$ffi/tests/c_smoke.rs" "$ffi/tests/failure_reason.rs" \
    "$ffi/tests/fs_mount.rs" "$ffi/tests/handler_smoke.rs" "$ffi/tests/instance_exec.rs" \
    "$ffi/tests/policy_fn.rs" "$ffi/tests/popen.rs" "$ffi/tests/protection.rs" \
    "$ffi/tests/restore.rs"

suite supervise uid501 "$src/$sup" \
    "$sup/src/lib.rs" "$sup/tests/supervise.rs"

suite oci root "$src/$oci" \
    "$oci/src/lib.rs" "$oci/src/main.rs" "$oci/tests/integration.rs" \
    "$oci/tests/test_init_reaper.rs" "$oci/tests/test_kill_all_delivery.rs" \
    "$oci/tests/test_process_groups.rs"

suite supervise_root root "$src/$sup" "$sup/tests/supervise_root.rs"

suite mediation_2uid root "$src/$sup" "$sup/tests/mediation_2uid.rs"

printf 'all phases ran\n'
