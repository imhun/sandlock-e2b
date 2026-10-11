#!/bin/sh
# Run the E2B suite in a container shaped like the deployed worker instead of a
# privileged test runner.
#
# Why this exists: `deploy/docker/Dockerfile.test-runner` gates have been run
# with `--privileged`, which grants the *test process* raw devices, module
# loading, ptrace of anything, mount, and every capability. That is not what a
# worker gets, so a suite that passes there can still be proving less than it
# looks like -- and anything the code quietly needs beyond the deployed
# permission set never shows up. This lane drops the privilege and starts from
# the historical deployed capset (deploy/k8s/worker.yaml before A6: SYS_ADMIN +
# NET_BIND_SERVICE, seccomp unconfined, running as root so E3.2 per-sandbox host
# uids and own identity are actually in play). A6 removed SYS_ADMIN from the
# manifests and A7 pins the no-SYS_ADMIN shape via
#   PROD_DROP_CAPS=SYS_ADMIN ./deploy/scripts/test-prod-shaped.sh
# so the cap list below is deliberately a superset today: the default run is
# the wider shape, the *pinned* production shape is the one with SYS_ADMIN
# dropped.
#
# Measured cost of dropping --privileged (2026-09-10, this host):
#   - Landlock ABI 8 and unprivileged user namespaces: fine.
#   - /dev/loop*: setup fails even with CAP_SYS_ADMIN + /dev/loop-control, so
#     the XFS prjquota scratch filesystem cannot be created in-lane. The suites
#     that hard-require it are deselected below -- explicitly, not silently
#     skipped -- and stay covered by the privileged lane. Only *real* XFS
#     prjquota dependencies belong in that list (see XFS_DESELECTS).
#
# Usage:  ./deploy/scripts/test-prod-shaped.sh [pytest args...]
#         IMAGE=... PROBE_EXTRA=... ./deploy/scripts/test-prod-shaped.sh
#         PROD_DROP_CAPS=SYS_ADMIN ./deploy/scripts/test-prod-shaped.sh
#         UNPRIVILEGED_PHASE=0 ...      skip the uid-65534 worker phase
#
# Two phases, because the deployment has two shapes:
#   1. root worker with the manifests' capability set -- E3.2 per-sandbox uids
#      and own-identity supervise slots are in play, which is where mediated
#      (chroot) sandboxes get their own mediator.
#   2. the *unprivileged* worker `docker-compose.prod.yml` runs today
#      (`user: "65534:65534"`, no CAP_SETUID): no uid pool, no slots, so the
#      sandbox is the worker's own identity and mediation runs in-process as
#      that uid. Phase 2 exists because deleting the `mediation_run_as`
#      downgrade tier could plausibly have broken exactly this shape, and a
#      suite that only ever runs as root would never notice.
set -eu
cd "$(dirname "$0")/../.."

IMAGE="${IMAGE:-e2b-sandlock-test:latest}"
# The shipped worker syscall filter (deploy/seccomp/README.md): the Docker
# default profile plus the two syscalls the sandbox-create path needs. This
# lane exists to reproduce the deployed shape, so it must run the profile that
# deploys -- `seccomp=unconfined` proved the code works under a filter the
# worker does not have. Override SECCOMP_PROFILE to widen it deliberately.
SECCOMP_PROFILE="${SECCOMP_PROFILE:-$(pwd)/deploy/seccomp/sandlock-worker.json}"
CAPS="--cap-drop ALL"
# PROD_DROP_CAPS=SYS_ADMIN,SYS_PTRACE (or any cap in the list below) narrows
# the lane to the pinned production shape. It has to *remove the cap from the
# --cap-add loop*, not just append a --cap-drop: this engine resolves --cap-add
# over --cap-drop regardless of argument order (measured 2026-09-11 on 29.4.0:
# `--cap-drop ALL --cap-add SYS_ADMIN --cap-drop SYS_ADMIN` still reports
# CapEff=0x…200000, i.e. SYS_ADMIN present, while `--cap-drop SYS_ADMIN` alone
# does drop it). The explicit --cap-drop below stays as belt and braces.
PROD_DROP_CAPS="$(printf '%s' "${PROD_DROP_CAPS:-}" | tr -d ' ')"
cap_is_dropped() {
    case ",$PROD_DROP_CAPS," in
        *",$1,"*) return 0 ;;
        *) return 1 ;;
    esac
}
# SYS_PTRACE is not decoration: writing a *child's* uid_map needs CAP_SETUID and
# ptrace access to that child, so a root worker without it cannot run the
# in-process RunAs path (measured: create fails with `sandlock_create failed`).
# Own identity needs no ptrace -- its slot already is the sandbox uid and self-maps --
# which is why chroot sandboxes keep working here even when the cap is dropped.
for cap in SYS_ADMIN SYS_PTRACE NET_BIND_SERVICE NET_RAW SYS_CHROOT CHOWN DAC_OVERRIDE \
           FOWNER FSETID KILL SETGID SETUID SETPCAP AUDIT_WRITE SETFCAP; do
    cap_is_dropped "$cap" || CAPS="$CAPS --cap-add $cap"
done

# Drop additional caps without editing the list above, e.g.
#   PROD_DROP_CAPS=SYS_ADMIN,SYS_PTRACE ./deploy/scripts/test-prod-shaped.sh
for drop in $(printf '%s' "$PROD_DROP_CAPS" | tr ',' ' '); do
    CAPS="$CAPS --cap-drop $drop"
done

# Deselected: they mount (or report on) a *real* XFS prjquota scratch
# filesystem, which this lane cannot create (see above). That is the only
# criterion. The quota unit suites (test_volume_quota,
# test_xfs_project_quota_agent, test_quota_agent_client, test_quota_maintenance)
# monkeypatch the filesystem detection and the xfs_quota subprocess, and
# tests/security/test_quota_enforcement.py no longer exists at all; A7 removed
# all five from an over-broad first cut, so the A5/A6 cases in them are back in
# the default gate.
XFS_DESELECTS="--ignore=tests/contract/test_volume_quota.py
--ignore=tests/contract/test_xfs_project_quota.py"

# Scope of E2B_TEST_STRICT_SKIPS=1 (measured by A5, corrected by A7): it only
# upgrades the *runner capability* markers listed in
# tests/conftest.py::_STRICT_SKIP_FORBIDDEN -- the live list is the source of
# truth; do not spell a count here (it grew when the buildkit/registry
# fixtures stopped skipping silently, and a number in a comment drifts).
# Today it covers the XFS-quota markers ("XFS quota integration requires",
# "does not support XFS project quota"), "npm is not installed",
# "needs NET_ADMIN", the workspace-ownership pair ("sandbox writes land owned
# by", "worker storage does not give the sandbox ownership") and the three
# "docker is required for ..." markers. An ordinary `pytest.mark.skipif`
# stays a skip.
# Both files above skip with the first marker, so dropping one from this list
# while the scratch filesystem is missing turns into an error, not a quiet
# smaller run.
#
# CAP_NET_ADMIN is for the *fixtures*: two network contracts put a synthetic
# origin address (198.18.0.99/32) on lo to have something real to allow/deny
# through. The worker itself never needs it -- which is why the same contracts
# pass here while the sandbox-side network enforcement stays under test.
EXTRA_CAPS="--cap-add NET_ADMIN"

# The unprivileged-port window. A worker runs *inside a container/pod netns*,
# and every runtime opens that window there: measured 2026-10-11 on this host,
# `docker run alpine sysctl net.ipv4.ip_unprivileged_port_start` answers 0 with
# no `sysctls:` declared. It matters because the shared-netns shape binds the
# per-sandbox wildcard DNS gateway on 127.0.1.x:53 *as the sandbox's own uid*
# (the own-identity slot's supervisor), which is `bind DNS gateway: Permission
# denied (os error 13)` -- N36 -- once the window is shut. This lane runs
# `--network host` so the
# in-process sandboxes share the runner's netns; that netns is the VM's, which
# starts at 1024. Reproduce the deployed window in it (idempotent, one
# privileged one-shot before the lane's own unprivileged runs start).
ensure_unprivileged_port_window() {
    if [ "${LANE_SKIP_PORT_WINDOW:-0}" = "1" ]; then
        echo "!! LANE_SKIP_PORT_WINDOW=1: leaving net.ipv4.ip_unprivileged_port_start" >&2
        echo "!! as is; the shared-netns wildcard DNS-gateway cases will fail with EPERM." >&2
        return 0
    fi
    # `--entrypoint sh` on both runs: the runner image's entrypoint prints its
    # own diagnostics (XFS/loop messages) on *stdout*, so a bare
    # ``docker run "$IMAGE" cat ...`` would capture those into the value and
    # compare an always-unequal string (measured 2026-10-11).
    current="$(docker run --rm --network host --entrypoint sh "$IMAGE" \
        -c 'cat /proc/sys/net/ipv4/ip_unprivileged_port_start' 2>/dev/null || true)"
    if [ "$current" = "0" ]; then
        return 0
    fi
    echo "==> opening the unprivileged-port window in this lane's netns:" >&2
    echo "    net.ipv4.ip_unprivileged_port_start: ${current:-unknown} -> 0" >&2
    docker run --rm --privileged --network host --entrypoint sh "$IMAGE" \
        -c 'echo 0 > /proc/sys/net/ipv4/ip_unprivileged_port_start'
}
ensure_unprivileged_port_window

# The image bakes in the multi-source default for E2B_REGISTRY_MIRRORS, but a
# run may want a *different* source -- typically the local registry:2 on
# 127.0.0.1:5080 with the images preloaded, which takes the public mirror chain
# out of the picture entirely (see docs/production-deployment-requirements.md,
# "公共镜像源"). Forward the host value only when it is set, so the baked-in
# default (and its "explicitly empty = pull directly" form) still applies
# otherwise. tests/conftest.py reads the same variable for buildkitd's mirror,
# so resolver and template builds never end up on different sources.
MIRRORS_ENV=""
if [ -n "${E2B_REGISTRY_MIRRORS:-}" ]; then
    MIRRORS_ENV="-e E2B_REGISTRY_MIRRORS=${E2B_REGISTRY_MIRRORS}"
else
    # Loud on purpose: without the local source the lane resolves the base
    # image through the public mirror chain, and the locally built
    # `python-mcp:3.14` is not in those mirrors' allowlists (measured
    # 2026-09-15: 205 failures/errors out of 1436, all of them
    # "this image is not in the allowlist" and the 428 warm_required cascade
    # behind it). The fix is the preloaded registry documented in
    # docs/production-deployment-requirements.md §2.6.1, i.e.
    #   E2B_REGISTRY_MIRRORS=registry-1.docker.io=127.0.0.1:5080 \
    #     ./deploy/scripts/test-prod-shaped.sh
    echo "!! E2B_REGISTRY_MIRRORS is unset: this run resolves base images through" >&2
    echo "!! the public mirror chain. Locally built python-mcp:3.14 is NOT in" >&2
    echo "!! those allowlists -- expect ~200 failures unless the local registry" >&2
    echo "!! on 127.0.0.1:5080 is preloaded and passed here (§2.6.1)." >&2
fi

# A *dead* mirror is worse than an unset one: the failure it produces looks like
# a code regression (every create whose fixture needs the base image answers 428
# warm_required) and it is spread across unrelated files. Measured 2026-10-11:
# the container behind 127.0.0.1:5080 had been removed, and one full run came
# back `30 failed / 2 errors` -- `warm_required` in 15 tracebacks, "registry
# endpoint http://127.0.0.1:5080/v2 unusable" in 32 -- with nothing in common
# between the files. The check below names it before pytest starts, because the
# fix is operational (docs/production-deployment-requirements.md §2.6.1).
# LANE_SKIP_MIRROR_CHECK=1 is the escape hatch for a deliberate cold/no-source
# run.
check_registry_mirrors() {
    [ "${LANE_SKIP_MIRROR_CHECK:-0}" = "1" ] && return 0
    [ -n "${E2B_REGISTRY_MIRRORS:-}" ] || return 0
    # `\n` on purpose: without the trailing newline `read` reports end-of-file
    # for a single-element (comma-free) value and the loop body never runs --
    # i.e. the check would silently pass for exactly the common case.
    printf '%s\n' "$E2B_REGISTRY_MIRRORS" | tr ',' '\n' | while read -r pair || [ -n "$pair" ]; do
        [ -n "$pair" ] || continue
        mirror="${pair#*=}"
        case "$mirror" in
            127.0.0.1:*|localhost:*) ;;
            *) continue ;;
        esac
        code="$(curl -s -o /dev/null -w '%{http_code}' -m 5 "http://$mirror/v2/" || true)"
        # 401 is a live registry that wants credentials; anything else (000
        # for a refused connection, 404, a proxy error) means "not usable".
        case "$code" in
            200|401) ;;
            *)
                echo "!! the registry mirror $mirror answers '${code:-no answer}': every create" >&2
                echo "!! that needs the base image will 428 warm_required across unrelated tests." >&2
                echo "!! Start it (and preload the full tag set) per" >&2
                echo "!! docs/production-deployment-requirements.md §2.6.1, or set" >&2
                echo "!! LANE_SKIP_MIRROR_CHECK=1 to run a deliberately cold lane." >&2
                return 1
                ;;
        esac
    done
}
check_registry_mirrors || exit 2

# The per-sandbox memory ceiling is a *deployment* value: the shipped stack
# runs 512MB (deploy/stack/.env, docs §2.4.8) while the code default is
# 1024MB. Both the control plane and the workers read it, and the boxed-quota
# contracts derive their allocation sizes from it, so a lane that means to
# reproduce the deployed shape has to forward it -- otherwise it silently
# proves the code default instead. Unset stays unset: the code default applies
# and the lane runs the 1 GiB shape the pre-2.4.8 contracts were written for.
MEMORY_ENV=""
if [ -n "${E2B_DEFAULT_MEMORY_MB:-}" ]; then
    MEMORY_ENV="-e E2B_DEFAULT_MEMORY_MB=${E2B_DEFAULT_MEMORY_MB}"
fi

# The per-sandbox PID namespace is opt-in per deployment (E2B_PID_NS, default
# false in envd_service/config.py), and the lane exists to reproduce the
# deployed shape -- so let a run pick the shape explicitly. Unset stays unset.
PIDNS_ENV=""
if [ -n "${E2B_PID_NS:-}" ]; then
    PIDNS_ENV="-e E2B_PID_NS=${E2B_PID_NS}"
fi

# The per-sandbox network namespace is a *deployment* shape too (E7.2): the
# shipped stack (`docker-compose.prod.yml`) and the k8s manifest run
# `E2B_ENABLE_NET_ISOLATION=true` + `E2B_FD_INJECT_CONNECT=true`, and this lane
# exists to reproduce the deployed shape -- of the *whole* suite, because the
# in-process control plane and worker read these two variables as their
# deployment default (envd_service/config.py, pinned by
# tests/unit/test_net_isolation_config.py). Forward them when both are set;
# without that the lane could only ever run the code default (shared netns)
# shape, which is why the documented netns-shaped full run (§2.4.5) was not
# reproducible from the script.
# `tests/contract/test_mcp_netns.py` is *not* the reason: it never needed this
# passthrough (the runner image bakes `E2B_TEST_NET_ISOLATION=1`, and the
# contract's own harness sets the worker's pair), so it runs the same way
# whether the pair arrives here or not. `E2B_TEST_NET_ISOLATION` is forwarded
# only so the shape stays explicit end to end.
# The two are a pair -- `create_app` refuses the single-switch shape by name (it
# would leave every sandbox loopback-only, i.e. "the network is down" with no
# error anywhere) -- so forward them together, and only when both are set.
# Unset stays unset: the code default is the shared-netns shape.
NETNS_ENV=""
if [ -n "${E2B_ENABLE_NET_ISOLATION:-}" ] && [ -n "${E2B_FD_INJECT_CONNECT:-}" ]; then
    NETNS_ENV="-e E2B_ENABLE_NET_ISOLATION=${E2B_ENABLE_NET_ISOLATION} -e E2B_FD_INJECT_CONNECT=${E2B_FD_INJECT_CONNECT} -e E2B_TEST_NET_ISOLATION=${E2B_TEST_NET_ISOLATION:-1}"
elif [ -n "${E2B_ENABLE_NET_ISOLATION:-}${E2B_FD_INJECT_CONNECT:-}" ]; then
    echo "!! E2B_ENABLE_NET_ISOLATION and E2B_FD_INJECT_CONNECT must be set together (create_app refuses the unpaired shape)" >&2
    exit 2
fi

# Image-cache location. Unset the lane uses config.py's default, a *relative*
# path under the CWD -- which on this lane is the repo bind mount, so the cache
# survives between runs and every create finds a warm image. Pointing it at a
# fresh (usually /tmp) path is the only way to reproduce the cold-lane shape
# (B6/N7: an official create without X-Sandbox-Id answers 428 warm_required),
# so forward it when the caller sets it; unset stays unset.
CACHE_ENV=""
if [ -n "${E2B_IMAGE_CACHE_DIR:-}" ]; then
    CACHE_ENV="-e E2B_IMAGE_CACHE_DIR=${E2B_IMAGE_CACHE_DIR}"
fi

# --- the run itself, in reusable containers ---------------------------------
#
# The suite runs *correctness only* here: `tests/perf` is the concurrency/
# latency suite, whose subject is a burst of 30-50 simultaneous sandboxes --
# a shape an admission-controlled lane cannot honestly declare (and one that
# made the lane's own `live_servers` pool the thing under test). It has its own
# entry point, `deploy/scripts/test-perf-shaped.sh`, so a correctness run is
# not gated on it and a perf run is not diluted by 3000 other tests.
#
# `--durations=25` and `PYTHONUNBUFFERED=1` (set when the containers are
# created) are there because this lane's wall clock is dominated by a handful
# of very slow cases, and by output buffering that makes a running suite look
# stuck when it is redirected to a file.
LANE_SECCOMP_PROFILE="$SECCOMP_PROFILE"
LANE_HOST_PROJECT="$(pwd)"
. "$(dirname "$0")/lib/lane-container.sh"
# The setup steps (warm-up, services) see the same shape env the suite will.
LANE_EXTRA_EXEC_ENV="$MIRRORS_ENV $MEMORY_ENV $PIDNS_ENV $NETNS_ENV $CACHE_ENV"

LANE_CONTAINER="${LANE_CONTAINER:-e2b-prod-shaped-lane}"
LANE_RUN_ID="${LANE_RUN_ID:-$(date +%Y%m%d-%H%M%S)-$$}"
#: What the suite run collects. `tests` (the whole suite, correctness only) is
#: the lane's meaning; `LANE_TARGETS=tests/security/test_x.py` is the iteration
#: lever -- the containers, the services and the warm-up are the same, so a
#: targeted run measures the same shape (and the same lane can be re-entered
#: cheaply, because nothing is created twice).
LANE_TARGETS="${LANE_TARGETS:-tests}"

lane_cleanup() {
    lane_container_reset_scratch "$LANE_CONTAINER-root" "$LANE_RUN_ID"
    if [ "${UNPRIVILEGED_PHASE:-1}" = "1" ]; then
        lane_container_reset_scratch \
            "$LANE_CONTAINER-nobody" "$LANE_RUN_ID-nobody"
    fi
}
trap lane_cleanup EXIT

lane_container_up "$LANE_CONTAINER-root" "$IMAGE" "$CAPS $EXTRA_CAPS" \
    "phase 1 (root worker)"
# Once per *container*, not per test: see lib/lane-container.sh and
# deploy/scripts/lane-warmup.py.
lane_container_warmup "$LANE_CONTAINER-root"
# Once per *lane*, not per pytest process: the two registries and buildkitd
# the fixtures read from the env (tests/conftest.py).
lane_services_up "$LANE_CONTAINER-root"
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
    $MEMORY_ENV \
    $PIDNS_ENV \
    $NETNS_ENV \
    $CACHE_ENV \
    "$LANE_CONTAINER-root" \
    pytest $LANE_TARGETS -q -p no:cacheprovider --durations=25 \
        --ignore=tests/perf $XFS_DESELECTS "$@"

if [ "${UNPRIVILEGED_PHASE:-1}" = "1" ]; then
    echo "==> phase 2: unprivileged worker (uid 65534, no privileged file-step path)"
    # The deployed non-root worker. Until C3 (open-issues N52) this phase
    # reproduced the *file-capability broker* shape -- uid 65534 plus the four
    # caps that let `/var/lib/e2b-priv/e2b-{slot-spawn,maint}` exec. Both
    # binaries are gone: the per-node agent performs every privileged file step
    # in the shipped shapes, and a worker with no agent keeps the in-process
    # (E5.1) shape -- no uid pool, no own-identity slot, the sandbox is the worker's
    # own identity and mediation runs in-process as that uid.
    #
    # That E5.1 shape is what this phase still exists to exercise: the unit
    # suites only ever run it through fakes, and a suite that only ever ran as
    # root would never notice it. `--cap-drop ALL` with no `--add` is now the
    # whole story (it was already true of the worker's own CapEff; the caps
    # existed only to open the file-capability gate).
    #
    # `tests/contract/test_nonroot_own_identity.py` is deliberately not in the list
    # any more: a non-root worker has no own-identity capability without the agent,
    # which is what that contract used to pin when the brokers supplied it.
    # Its own reusable container, created with `--cap-drop ALL` and no `--add`:
    # the whole point of this phase is that *nothing* is granted, so it cannot
    # share phase 1's container (caps are fixed when a container is created).
    lane_container_up "$LANE_CONTAINER-nobody" "$IMAGE" "--cap-drop ALL" \
        "phase 2 (uid 65534 worker)"
    # The shared rootfs cache belongs to phase 1's *root* process, so this
    # container's copy of the path is root-owned (or missing) and uid 65534
    # cannot traverse it: prepared root-side here, in the lib, because the
    # suite's `docker exec` shape is pinned by
    # tests/unit/test_prod_shaped_lane_netns_passthrough.py (two execs of the
    # *suite*, not three).
    lane_container_prepare_rootfs "$LANE_CONTAINER-nobody"
    # shellcheck disable=SC2086
    docker exec -u 65534:65534 \
        -e HOME=/tmp -e TMPDIR=/tmp \
        -e E2B_TEST_TMP_ROOT="$(lane_container_scratch "$LANE_RUN_ID-nobody")" \
        -e E2B_BASE_IMAGE="${PHASE2_BASE_IMAGE:-python:3.11-slim}" \
        $SERVICES_ENV \
        $MIRRORS_ENV \
        $MEMORY_ENV \
        $PIDNS_ENV \
        $NETNS_ENV \
        $CACHE_ENV \
        "$LANE_CONTAINER-nobody" \
        pytest tests/security/test_template_isolation.py \
            tests/security/test_sandlock_isolation.py \
            tests/unit/test_sandlock_executor_own_identity.py \
            tests/unit/test_policy_mapping.py -q -p no:cacheprovider \
            --durations=25 "$@"
        # ^ E2B_BASE_IMAGE is deliberately not inherited: `python-mcp:3.14` is a
        # locally built image that the registry mirrors refuse (403 not in the
        # allowlist), and phase 1 only resolves it because its rootfs is already
        # in the harness cache that uid 65534 cannot write.
fi
