#!/usr/bin/env bash
# Test-runner entrypoint: give the suite the storage it asserts against.
#
# The XFS project-quota tests (and the per-sandbox uid ownership assertions)
# only mean something on a filesystem that actually implements project quota
# and host uid ownership. The container root is overlay (and /workspace is a
# virtiofs bind mount on an OrbStack/Docker Desktop host, where chown is a
# no-op), so without this the suite would skip 10 quota tests and measure
# ownership against a filesystem that cannot express it.
#
# Recipe (docs/sandbox-disk-quota.md §4): a loop-mounted XFS image with
# prjquota. Needs a privileged container. When that is unavailable the runner
# still works, it just reports the gates as unsatisfied -- and with
# E2B_TEST_STRICT_SKIPS=1 an unsatisfied gate fails loudly instead of
# disappearing into the skip list.
set -uo pipefail

XFS_MOUNT="${E2B_XFS_TEST_MOUNT:-/var/lib/e2b-sandboxes}"
XFS_IMAGE="${E2B_XFS_IMAGE:-/var/lib/e2b-test-xfs.img}"
XFS_SIZE_MB="${E2B_XFS_IMAGE_MB:-6144}"
LOOP_DEV=""

cleanup() {
    # Only detach what this container bound; loop devices are shared with the
    # host and with sibling containers, so anything else is not ours to touch.
    [ -n "${LOOP_DEV}" ] && losetup -d "${LOOP_DEV}" >/dev/null 2>&1
    return 0
}
trap cleanup EXIT

# /dev/loopN nodes are not created by udev inside a container, so `losetup -f`
# can pick a minor with no node and fail ("device node /dev/loopN is lost",
# typically after other runs leaked loop bindings). Allocate explicitly and
# mknod the node when it is missing.
attach_loop() {
    local image="$1" dev minor candidate
    if dev="$(losetup -f --show "${image}" 2>/dev/null)" && [ -b "${dev}" ]; then
        printf '%s' "${dev}"
        return 0
    fi
    for minor in $(seq 0 63); do
        candidate="/dev/loop${minor}"
        [ -e "${candidate}" ] || mknod "${candidate}" b 7 "${minor}" 2>/dev/null || continue
        if losetup "${candidate}" "${image}" 2>/dev/null; then
            printf '%s' "${candidate}"
            return 0
        fi
    done
    return 1
}

prepare_xfs() {
    if mount | grep -q " ${XFS_MOUNT} "; then
        echo "test-runner: reusing the XFS mount at ${XFS_MOUNT}"
        return 0
    fi
    [ -e /dev/loop-control ] || { echo "test-runner: no /dev/loop-control: XFS gates unavailable"; return 1; }
    command -v mkfs.xfs >/dev/null || { echo "test-runner: xfsprogs missing: XFS gates unavailable"; return 1; }
    [ "$(id -u)" = "0" ] || { echo "test-runner: not root: cannot set up XFS"; return 1; }

    mkdir -p "${XFS_MOUNT}"
    if [ ! -f "${XFS_IMAGE}" ]; then
        echo "test-runner: creating ${XFS_IMAGE} (${XFS_SIZE_MB}MB sparse)"
        dd if=/dev/zero of="${XFS_IMAGE}" bs=1M count=0 seek="${XFS_SIZE_MB}" status=none || return 1
    fi
    LOOP_DEV="$(attach_loop "${XFS_IMAGE}")" || {
        echo "test-runner: no free loop device: XFS gates unavailable"
        return 1
    }
    if ! blkid "${LOOP_DEV}" >/dev/null 2>&1; then
        mkfs.xfs -f "${LOOP_DEV}" >/dev/null || { echo "test-runner: mkfs.xfs failed"; return 1; }
    fi
    mount -t xfs -o prjquota "${LOOP_DEV}" "${XFS_MOUNT}" || {
        echo "test-runner: mounting ${LOOP_DEV} on ${XFS_MOUNT} failed"
        return 1
    }
    echo "test-runner: XFS with prjquota mounted at ${XFS_MOUNT} (${LOOP_DEV})"
    return 0
}

if prepare_xfs; then
    export E2B_XFS_QUOTA_INTEGRATION=1
    # Sandbox workspaces (and therefore the ownership/quota assertions) land on
    # that XFS too, matching a real worker's host-local storage.
    export E2B_TEST_TMP_ROOT="${XFS_MOUNT}/_test-runtime"
    mkdir -p "${E2B_TEST_TMP_ROOT}"
fi

exec "$@"
