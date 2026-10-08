#!/bin/sh
# Production-shape worker smoke: exercise the worker-side sandlock stack the
# way the worker is actually deployed: **uid 65534 with exactly the four
# bounding-set caps the file-capability brokers need**, seccomp unconfined
# (sandlock installs its own seccomp filters and needs a user namespace for the
# uid/gid map, which Docker's default profile would block), image rootfs pulled
# directly from the OCI registry — then run representative tests:
#
#   * the deployed identity shape: pooled host uid + own-identity slot, uid 0 inside
#     the namespace, host-side owner = the pooled uid (§2.4.1 / 决定 #1);
#   * a real image-rootfs sandbox (chroot + CA splice)
#   * the SOCKS5 egress on-behalf path (sandbox network)
#
# Why these flags (F1 / fix round 2, task-Z F9): the earlier form ran the image
# as *root with Docker's default caps* — neither the deployment shape nor the
# privileged test runner (no SYS_PTRACE), so the in-process RunAs could not map
# a uid and every case died in `sandlock_create failed`. It also asserted the
# old shared-uid semantics (`host uid 1000 mapped to 0`). The deployment shape
# is: non-root + `--cap-drop ALL` + the four broker caps in BND; the sandbox's
# host uid is the pooled uid and its namespace maps it to 0.
#
# Usage:
#   ./deploy/scripts/smoke-prod-worker.sh [image]
set -eu

IMAGE="${1:-e2b-sandlock-test:latest}"
BASE_IMAGE="${E2B_BASE_IMAGE:-python:3.14-slim}"
# The shipped worker syscall filter (deploy/seccomp/README.md). This smoke is
# meant to be the deployment shape, so it uses the profile the manifests do
# rather than `seccomp=unconfined`.
SECCOMP_PROFILE="${SECCOMP_PROFILE:-$(pwd)/deploy/seccomp/sandlock-worker.json}"
# Scratch that uid 65534 can actually write (the image's own
# /var/lib/e2b-test-runtime is root-owned); container-native, so ownership and
# 0770 workspaces still behave like the deployment.
TEST_TMP_ROOT="${E2B_TEST_TMP_ROOT:-/tmp/e2b-test-runtime}"

echo "==> production-shape worker smoke ($IMAGE, E2B_BASE_IMAGE=$BASE_IMAGE)"
docker run --rm \
    --user 65534:65534 \
    --cap-drop ALL \
    --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add DAC_OVERRIDE \
    --security-opt seccomp="$SECCOMP_PROFILE" \
    -e HOME=/tmp -e TMPDIR=/tmp \
    -e E2B_TEST_TMP_ROOT="$TEST_TMP_ROOT" \
    -e E2B_BASE_IMAGE="$BASE_IMAGE" \
    -e E2B_HOST_PROJECT="$(pwd)" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$(pwd):/workspace" -w /workspace \
    "$IMAGE" pytest \
        tests/security/test_fork_network_features.py::test_sandbox_child_runs_unprivileged \
        tests/security/test_fork_network_features.py::test_https_mitm_ca_spliced_in_image_rootfs \
        tests/security/test_egress_proxy.py::test_egress_proxy_tunnels_tcp_after_filter \
        -q -p no:cacheprovider
