#!/bin/sh
# Run the E2B suite in a container shaped like the deployed worker instead of a
# privileged test runner.
#
# Why this exists: `deploy/docker/Dockerfile.test-runner` gates have been run
# with `--privileged`, which grants the *test process* raw devices, module
# loading, ptrace of anything, mount, and every capability. That is not what a
# worker gets, so a suite that passes there can still be proving less than it
# looks like -- and anything the code quietly needs beyond the deployed
# permission set never shows up. This lane drops the privilege and keeps the
# capabilities the production manifests declare (deploy/k8s/worker.yaml:
# SYS_ADMIN + NET_BIND_SERVICE, seccomp unconfined, running as root so E3.2
# per-sandbox host uids and route B are actually in play).
#
# Measured cost of dropping --privileged (2026-09-10, this host):
#   - Landlock ABI 8 and unprivileged user namespaces: fine.
#   - /dev/loop*: setup fails even with CAP_SYS_ADMIN + /dev/loop-control, so
#     the XFS prjquota scratch filesystem cannot be created in-lane. The suites
#     that hard-require it are deselected below -- explicitly, not silently
#     skipped -- and stay covered by the privileged lane.
#
# Usage:  ./deploy/scripts/test-prod-shaped.sh [pytest args...]
#         IMAGE=... PROBE_EXTRA=... ./deploy/scripts/test-prod-shaped.sh
set -eu
cd "$(dirname "$0")/../.."

IMAGE="${IMAGE:-e2b-sandlock-test:latest}"
CAPS="--cap-drop ALL"
# SYS_PTRACE is not decoration: writing a *child's* uid_map needs CAP_SETUID and
# ptrace access to that child, so a root worker without it cannot run the
# in-process RunAs path (measured: create fails with `sandlock_create failed`).
# Route B needs no ptrace -- its slot already is the sandbox uid and self-maps --
# which is why chroot sandboxes keep working here even when the cap is dropped.
for cap in SYS_ADMIN SYS_PTRACE NET_BIND_SERVICE NET_RAW SYS_CHROOT CHOWN DAC_OVERRIDE \
           FOWNER FSETID KILL SETGID SETUID SETPCAP AUDIT_WRITE SETFCAP; do
    CAPS="$CAPS --cap-add $cap"
done

# Deselected: they need the XFS prjquota scratch filesystem, which this lane
# cannot create (see above). Strict skips stay ON, so forgetting one here turns
# into an error rather than a silently smaller run.
XFS_DESELECTS="--ignore=tests/contract/test_volume_quota.py
--ignore=tests/contract/test_xfs_project_quota.py
--ignore=tests/unit/test_volume_quota.py
--ignore=tests/unit/test_xfs_project_quota_agent.py
--ignore=tests/unit/test_quota_agent_client.py
--ignore=tests/unit/test_quota_maintenance.py
--ignore=tests/security/test_quota_enforcement.py"

# CAP_NET_ADMIN is for the *fixtures*: two network contracts put a synthetic
# origin address (198.18.0.99/32) on lo to have something real to allow/deny
# through. The worker itself never needs it -- which is why the same contracts
# pass here while the sandbox-side network enforcement stays under test.
EXTRA_CAPS="--cap-add NET_ADMIN"

# shellcheck disable=SC2086
docker run --rm --init --network host \
    $CAPS $EXTRA_CAPS --security-opt seccomp=unconfined \
    --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$(pwd)" \
    -e E2B_TEST_STRICT_SKIPS=1 \
    -e E2B_BASE_IMAGE="${E2B_BASE_IMAGE:-python-mcp:3.14}" \
    -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    "$IMAGE" \
    pytest tests --perf -q -p no:cacheprovider $XFS_DESELECTS "$@"
