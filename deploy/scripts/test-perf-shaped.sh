#!/bin/sh
# The concurrency/latency suite, in the deployed worker's shape -- on its own.
#
# Split out of `test-prod-shaped.sh` on 2026-10-11 (operator decision): the
# correctness lane no longer runs `tests/perf`, and this script no longer runs
# 3000 other tests around it.
#
# The reason is what the perf suite *is*: `tests/perf/test_latency.py` asks for
# a burst of 30-50 simultaneous sandboxes and cites latencies in the tens of
# milliseconds. In the correctness lane it failed every run with
# `503: No resources available`, because the in-process node's declared slice
# (`IN_PROCESS_NODE_DEFAULT_TOTALS`: 8192 MiB / 400 CPU points / 2048
# processes) admits four concurrent sandboxes -- admission control working, and
# a subject the perf suite does not mean to measure. `live_servers` therefore
# declares a pool wide enough for the burst (it is the SDK harness; the gates
# themselves have their own fixtures), and this script is where that harness's
# bursts belong.
#
# Usage:   ./deploy/scripts/test-perf-shaped.sh [pytest args...]
#          ./deploy/scripts/test-prod-shaped.sh       # correctness only
#
# Container reuse, the seccomp profile, the caps and the image mirror are the
# same as the correctness lane -- see deploy/scripts/lib/lane-container.sh.
set -eu
cd "$(dirname "$0")/../.."

IMAGE="${IMAGE:-e2b-sandlock-test:latest}"
SECCOMP_PROFILE="${SECCOMP_PROFILE:-$(pwd)/deploy/seccomp/sandlock-worker.json}"

CAPS="--cap-drop ALL"
for cap in SYS_ADMIN SYS_PTRACE NET_BIND_SERVICE NET_RAW SYS_CHROOT CHOWN DAC_OVERRIDE \
           FOWNER FSETID KILL SETGID SETUID SETPCAP AUDIT_WRITE SETFCAP; do
    CAPS="$CAPS --cap-add $cap"
done
# NET_ADMIN is for the *fixtures*: the perf suite's sandboxes resolve the base
# image through the local registry mirror and the harness binds ports.
CAPS="$CAPS --cap-add NET_ADMIN"

MIRRORS_ENV=""
if [ -n "${E2B_REGISTRY_MIRRORS:-}" ]; then
    MIRRORS_ENV="-e E2B_REGISTRY_MIRRORS=${E2B_REGISTRY_MIRRORS}"
else
    echo "!! E2B_REGISTRY_MIRRORS is unset: base images resolve through the" >&2
    echo "!! public mirror chain, and the locally built python-mcp:3.14 is not" >&2
    echo "!! in those allowlists (docs/production-deployment-requirements.md §2.6.1)." >&2
fi

LANE_SECCOMP_PROFILE="$SECCOMP_PROFILE"
LANE_HOST_PROJECT="$(pwd)"
. "$(dirname "$0")/lib/lane-container.sh"
LANE_EXTRA_EXEC_ENV="$MIRRORS_ENV"

LANE_CONTAINER="${LANE_CONTAINER:-e2b-prod-shaped-perf}"
LANE_RUN_ID="${LANE_RUN_ID:-$(date +%Y%m%d-%H%M%S)-$$}"

lane_cleanup() {
    lane_container_reset_scratch "$LANE_CONTAINER" "$LANE_RUN_ID"
}
trap lane_cleanup EXIT

lane_container_up "$LANE_CONTAINER" "$IMAGE" "$CAPS" "perf (root worker)"
lane_container_warmup "$LANE_CONTAINER"
lane_services_up "$LANE_CONTAINER"
SERVICES_ENV="$(lane_services_env)"

# No readiness-deadline override: the pool keeps its deployment default (30 s).
# The reason the lane used to need 180 s here was a real bug -- a hand-rolled
# ``clone3`` released the GIL around the syscall, so a child whose parent's other
# thread held the GIL at that instant parked forever in ctypes'
# ``PyEval_RestoreThread`` before it ever exec'd (fixed 2026-10-11 in
# ``envd_service/identity_grant.py``: ``PyDLL`` + an allocation-free child path;
# first chroot launch went from 31/92/184 s -- or a hang -- to 0.07-0.095 s in
# four fresh containers). Raising the deadline would only have hidden it.
# shellcheck disable=SC2086
docker exec \
    -e E2B_TEST_TMP_ROOT="$(lane_container_scratch "$LANE_RUN_ID")" \
    -e E2B_BASE_IMAGE="${E2B_BASE_IMAGE:-python-mcp:3.14}" \
    -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    $SERVICES_ENV \
    $MIRRORS_ENV \
    "$LANE_CONTAINER" \
    pytest tests/perf --perf -q -p no:cacheprovider --durations=25 "$@"
