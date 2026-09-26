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
# uids and route B are actually in play). A6 removed SYS_ADMIN from the
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
#      and route-B supervise slots are in play, which is where mediated
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
# Route B needs no ptrace -- its slot already is the sandbox uid and self-maps --
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
# shipped stack and the k8s manifest run `E2B_ENABLE_NET_ISOLATION=true` +
# `E2B_FD_INJECT_CONNECT=true`, and this lane exists to reproduce the deployed
# shape. The two are a pair -- `create_app` refuses the single-switch shape by
# name (it would leave every sandbox loopback-only, i.e. "the network is
# down" with no error anywhere) -- so forward them together, and only when
# both are set. Unset stays unset: the code default is the shared-netns shape.
# `E2B_TEST_NET_ISOLATION` is what un-skips `tests/contract/test_mcp_netns.py`
# (three cases); without it a "netns shape" run is green for the wrong reason.
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

# shellcheck disable=SC2086
docker run --rm --init --network host \
    $CAPS $EXTRA_CAPS --security-opt seccomp="$SECCOMP_PROFILE" \
    --security-opt apparmor=unconfined \
    -e E2B_HOST_PROJECT="$(pwd)" \
    -e E2B_TEST_STRICT_SKIPS=1 \
    -e E2B_BASE_IMAGE="${E2B_BASE_IMAGE:-python-mcp:3.14}" \
    -e E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2 \
    $MIRRORS_ENV \
    $MEMORY_ENV \
    $PIDNS_ENV \
    $NETNS_ENV \
    $CACHE_ENV \
    -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
    -v "$(pwd):/workspace" -w /workspace \
    "$IMAGE" \
    pytest tests --perf -q -p no:cacheprovider $XFS_DESELECTS "$@"

if [ "${UNPRIVILEGED_PHASE:-1}" = "1" ]; then
    echo "==> phase 2: unprivileged worker (uid 65534 + the file-capability brokers)"
    # Track F (Task F1): the deployed non-root worker gets per-sandbox host uids
    # and route-B slots from the two file-capability brokers
    # (/var/lib/e2b-priv/e2b-slot-spawn, e2b-maint). File capabilities are a
    # *subset* of the container's bounding set or the exec is refused with
    # EPERM (measured), so this phase has to declare the same four caps the
    # manifests do -- with `--cap-drop ALL` alone the lane would "prove" that
    # the mechanism is unusable while the same image's brokers work in a real
    # deployment. The worker's own CapEff stays 0 (no ambient caps for a
    # non-root process); the capabilities only ever arrive via the broker
    # binaries.
    # shellcheck disable=SC2086
    docker run --rm --init --network host --user 65534:65534 \
        --cap-drop ALL \
        --cap-add SETUID --cap-add SETGID --cap-add CHOWN --cap-add DAC_OVERRIDE \
        --security-opt seccomp="$SECCOMP_PROFILE" \
        --security-opt apparmor=unconfined \
        -e HOME=/tmp -e TMPDIR=/tmp \
        -e E2B_HOST_PROJECT="$(pwd)" \
        -e E2B_TEST_STRICT_SKIPS=1 \
        -e E2B_BASE_IMAGE="${PHASE2_BASE_IMAGE:-python:3.11-slim}" \
        $MIRRORS_ENV \
        $MEMORY_ENV \
        $PIDNS_ENV \
    $NETNS_ENV \
        $CACHE_ENV \
        -v "$(pwd):/workspace" -w /workspace \
        "$IMAGE" \
        pytest tests/security/test_template_isolation.py \
            tests/security/test_sandlock_isolation.py \
            tests/unit/test_sandlock_executor_route_b.py \
            tests/unit/test_policy_mapping.py \
            tests/contract/test_nonroot_route_b.py -q -p no:cacheprovider "$@"
        # ^ E2B_BASE_IMAGE is deliberately not inherited: `python-mcp:3.14` is a
        # locally built image that the registry mirrors refuse (403 not in the
        # allowlist), and phase 1 only resolves it because its rootfs is already
        # in the harness cache that uid 65534 cannot write.
fi
