#!/bin/sh
# The whole suite in the production-shaped root worker with E2B_BASE_IMAGE="" --
# the "gate B" half of N15's acceptance (`test-prod-shaped.sh` cannot express an
# empty base image: its `-e E2B_BASE_IMAGE="${E2B_BASE_IMAGE:-python-mcp:3.14}"`
# turns an empty value back into the default).
#
# Same caps, same env as that script's phase 1, plus the registry mirror the
# image-rootfs cases in the tree still need.
set -eu
cd /Users/polus/project/ai/sandlock-e2b
log="$1"
# Same knob as `gateA-full.sh`/`gateB-pure-rootfs.sh`: the image bakes in
# `wheels/fork/*.whl`, so a lane that must be same-source with the wheels on
# disk has to name the image rather than fall back to the shared `:latest`
# (`deploy/scripts/build-test-image.sh` header, pitfalls §B7).
IMAGE="${E2B_TEST_IMAGE:-e2b-sandlock-test:latest}"
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
    -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    -e E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    -e E2B_REAL_ROOT=0 \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    "$IMAGE" \
    pytest tests --perf -q -p no:cacheprovider \
        --ignore=tests/contract/test_volume_quota.py \
        --ignore=tests/contract/test_xfs_project_quota.py \
        > "$log" 2>&1
