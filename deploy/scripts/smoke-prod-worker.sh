#!/bin/sh
# Production-shape worker smoke: exercise the worker-side sandlock stack the
# way deploy/compose/docker-compose.prod.yml deploys it — NON-privileged
# container, seccomp
# unconfined (sandlock installs its own seccomp filters and needs a user
# namespace for the uid/gid map, which Docker's default profile would block),
# image rootfs pulled directly from the OCI registry (no Docker daemon) — then
# run representative tests:
#
#   * a confined sandbox process (no-root child)
#   * a real image-rootfs sandbox (chroot + CA splice)
#   * the SOCKS5 egress on-behalf path (sandbox network)
#
# Usage:
#   ./deploy/scripts/smoke-prod-worker.sh [image]
set -eu

IMAGE="${1:-e2b-sandlock-test:latest}"
BASE_IMAGE="${E2B_BASE_IMAGE:-python:3.14-slim}"

echo "==> production-shape worker smoke ($IMAGE, E2B_BASE_IMAGE=$BASE_IMAGE)"
docker run --rm \
    --security-opt seccomp=unconfined \
    -e E2B_BASE_IMAGE="$BASE_IMAGE" \
    -e E2B_HOST_PROJECT="$(pwd)" \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$(pwd):/workspace" -w /workspace \
    "$IMAGE" pytest \
        tests/security/test_fork_network_features.py::test_sandbox_child_runs_unprivileged \
        tests/security/test_fork_network_features.py::test_https_mitm_ca_spliced_in_image_rootfs \
        tests/security/test_egress_proxy.py::test_egress_proxy_tunnels_tcp_after_filter \
        -q -p no:cacheprovider
