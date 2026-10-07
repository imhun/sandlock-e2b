#!/bin/sh
# Sequential re-verification (f31) on the final bytes, one container per phase.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
IMG=e2b-sandlock-test:latest
SOCK="$HOME/.orbstack/run/docker.sock"
stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }
wait_marker() { while [ ! -f "$1" ]; do sleep 10; done; echo "== $2 $(stamp)"; }

priv() {
    name="$1"; shift
    docker run -d --name "$name" --privileged --network host --security-opt label=disable \
        -v "$ROOT:/workspace" -v "$SOCK:/var/run/docker.sock" \
        -e E2B_HOST_PROJECT="$ROOT" \
        -e E2B_REGISTRY_MIRRORS='registry-1.docker.io=docker.m.daocloud.io|docker.1ms.run' \
        -e E2B_TEST_TMP_ROOT=/var/lib/e2b-test-runtime \
        -e E2B_TEST_NET_ISOLATION=1 -e E2B_TEST_STRICT_SKIPS=1 \
        "$IMG" "$@" >/dev/null
}

echo "### focused $(stamp)"
priv f31-focused bash -c "cd /workspace && pytest tests/security/test_template_isolation.py tests/security/test_sandlock_isolation.py tests/security/test_worker_nonroot.py tests/contract/test_own_identity_executor.py tests/contract/test_own_identity_slot_pool.py tests/contract/test_uid_permissions.py tests/security/test_uid_isolation.py tests/unit/test_sandlock_executor_own_identity.py tests/unit/test_policy_mapping.py tests/unit/test_own_identity_wiring.py -q -p no:cacheprovider --tb=short > tmp/f31-focused.log 2>&1; echo FOCUSED-EXIT=\$? >> tmp/f31-focused.log; echo done > tmp/f31-focused.done"
wait_marker "$ROOT/tmp/f31-focused.done" "focused done"

echo "### gate A $(stamp)"
priv f31-gate-a bash -c "cd /workspace && E2B_BASE_IMAGE=python-mcp:3.14 E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 pytest tests --perf -q -p no:cacheprovider > tmp/f31-gate-a.log 2>&1; echo GATE-A-EXIT=\$? >> tmp/f31-gate-a.log; echo done > tmp/f31-gate-a.done"
wait_marker "$ROOT/tmp/f31-gate-a.done" "gate A done"

echo "### gate B $(stamp)"
priv f31-gate-b bash -c "cd /workspace && E2B_BASE_IMAGE= E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 pytest tests --perf -q -p no:cacheprovider > tmp/f31-gate-b.log 2>&1; echo GATE-B-EXIT=\$? >> tmp/f31-gate-b.log; echo done > tmp/f31-gate-b.done"
wait_marker "$ROOT/tmp/f31-gate-b.done" "gate B done"

echo "### prod phase 1 $(stamp)"
docker run -d --name f31-prod1 --init --network host \
    --cap-drop ALL --cap-add SYS_ADMIN --cap-add SYS_PTRACE --cap-add NET_BIND_SERVICE \
    --cap-add NET_RAW --cap-add SYS_CHROOT --cap-add CHOWN --cap-add DAC_OVERRIDE \
    --cap-add FOWNER --cap-add FSETID --cap-add KILL --cap-add SETGID --cap-add SETUID \
    --cap-add SETPCAP --cap-add AUDIT_WRITE --cap-add SETFCAP --cap-add NET_ADMIN \
    --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$ROOT" -e E2B_TEST_STRICT_SKIPS=1 \
    -e E2B_BASE_IMAGE=python-mcp:3.14 -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    -v "$SOCK:/var/run/docker.sock" -v "$ROOT:/workspace" -w /workspace "$IMG" \
    sh -c 'pytest tests --perf -q -p no:cacheprovider --ignore=tests/contract/test_volume_quota.py --ignore=tests/contract/test_xfs_project_quota.py > tmp/f31-prod1.log 2>&1; echo PROD1-EXIT=$? >> tmp/f31-prod1.log; echo done > tmp/f31-prod1.done' >/dev/null
wait_marker "$ROOT/tmp/f31-prod1.done" "prod phase 1 done"

echo "### prod phase 2 (uid 65534) $(stamp)"
docker run -d --name f31-prod2 --init --network host --user 65534:65534 \
    --cap-drop ALL --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
    -e HOME=/tmp -e TMPDIR=/tmp -e E2B_HOST_PROJECT="$ROOT" -e E2B_TEST_STRICT_SKIPS=1 \
    -e E2B_BASE_IMAGE=python:3.11-slim \
    -v "$ROOT:/workspace" -w /workspace "$IMG" \
    pytest tests/security/test_template_isolation.py tests/security/test_sandlock_isolation.py tests/unit/test_sandlock_executor_own_identity.py tests/unit/test_policy_mapping.py -q -p no:cacheprovider --tb=short >/dev/null
while ! [ -f "$ROOT/tmp/f31-prod2.done" ]; do
    if ! docker ps --format '{{.Names}}' | grep -qx f31-prod2; then
        docker logs f31-prod2 > "$ROOT/tmp/f31-prod2.log" 2>&1
        echo done > "$ROOT/tmp/f31-prod2.done"
        echo "== prod phase 2 $(stamp)"
    else
        sleep 10
    fi
done
echo "### ALL DONE $(stamp)" > "$ROOT/tmp/f31-all.done"
