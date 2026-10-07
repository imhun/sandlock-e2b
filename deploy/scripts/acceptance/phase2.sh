#!/bin/sh
# test-prod-shaped.sh's phase 2 (the unprivileged worker), runnable on its own.
set -eu
cd "$(cd "$(dirname "$0")/../../.." && pwd)"
log="$1"
# Same `E2B_TEST_IMAGE` knob as the gate scripts: the image bakes in
# `wheels/fork/*.whl`, so a same-source lane must name it instead of taking the
# shared `:latest` (pitfalls §B7).
IMAGE="${E2B_TEST_IMAGE:-e2b-sandlock-test:latest}"
SECCOMP_PROFILE="$(pwd)/deploy/seccomp/sandlock-worker.json"
docker run --rm --init --network host --user 65534:65534 \
    --cap-drop ALL \
    --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add DAC_OVERRIDE \
    --security-opt seccomp="$SECCOMP_PROFILE" \
    --security-opt apparmor=unconfined \
    -e HOME=/tmp -e TMPDIR=/tmp \
    -e E2B_HOST_PROJECT="$(pwd)" \
    -e E2B_TEST_STRICT_SKIPS=1 \
    -e E2B_BASE_IMAGE=python:3.11-slim \
    -e E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    -v "$(pwd):/workspace" -w /workspace \
    "$IMAGE" \
    python -m pytest -q -p no:cacheprovider \
        tests/security/test_template_isolation.py \
        tests/security/test_sandlock_isolation.py \
        tests/unit/test_sandlock_executor_own_identity.py \
        tests/unit/test_policy_mapping.py \
        tests/contract/test_nonroot_own_identity.py > "$log" 2>&1
