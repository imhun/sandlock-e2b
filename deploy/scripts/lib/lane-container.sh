#!/bin/sh
# Long-lived, reusable lane containers (sourced by the shaped-lane scripts).
#
# Why: a *container start* is cheap (0.3 s), but the first chroot-sandbox
# launch inside a container is not -- measured 2026-10-11, the first
# `sandbox start()` in a fresh container waits out two 15 s slot-verb
# timeouts (~31 s) and every later one is 0.07 s. That one-time cost is why a
# fresh container per phase/per iteration is expensive, and why it can also
# *fail* a whole test file: a security file's `require_sandlock` fixture
# builds a sandbox, and the 30 s readiness deadline loses the race against a
# 31 s first launch (11 files errored that way in one full run).
#
# So the lane keeps its containers and reuses them:
#   * `<name>-root`   -- phase 1's capset (the one the manifests grant),
#   * `<name>-nobody` -- phase 2's `--cap-drop ALL`, no `--add`;
# each is recreated only when the image changed or `LANE_RECREATE=1` is set.
#
# Per-run state is kept out of the container: each run gets its own
# `E2B_TEST_TMP_ROOT` (so a run never inherits another run's uid-pool markers,
# slot scratch or sandbox trees) while the *extracted image rootfs cache* lives
# in a stable directory, so it is paid once per image per container and not
# once per call (`E2B_TEST_ROOTFS_DIR`).

#: Where the run's scratch and the stable rootfs cache live inside the
#: container. The scratch goes on the container's own disk (deleted at the end
#: of the run); the rootfs cache is deliberately *outside* it.
LANE_ROOTFS_DIR="${LANE_ROOTFS_DIR:-/var/lib/lane/rootfs}"

#: `-e ...` block the callers fill in (mirrors, memory, cache dir) so the
#: *setup* steps see the same shape the suite will: the warm-up resolves an
#: image and buildkitd bakes the mirror list into its config, and both are
#: wrong without them (measured: the warm-up went to the public mirror chain and
#: got a 403).
LANE_EXTRA_EXEC_ENV="${LANE_EXTRA_EXEC_ENV:-}"

lane_container_reusable() {
    # $1 = container name. Reusable when it is running, on this exact image,
    # and the caller did not ask for a rebuild.
    [ "${LANE_RECREATE:-0}" = "1" ] && return 1
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = "true" ] ||
        return 1
    [ "$(docker inspect -f '{{.Image}}' "$1" 2>/dev/null)" = "$(docker image inspect -f '{{.Id}}' "$2")" ]
}

lane_container_up() {
    # $1 = container name, $2 = image, $3 = extra docker args (caps), $4 = label
    name="$1"
    image="$2"
    caps="$3"
    label="$4"
    if lane_container_reusable "$name" "$image"; then
        echo "==> reusing $label container ($name, image unchanged)" >&2
        return 0
    fi
    docker rm -f "$name" >/dev/null 2>&1 || true
    echo "==> creating $label container ($name)" >&2
    # shellcheck disable=SC2086
    docker run -d --init --network host --name "$name" \
        $caps \
        --security-opt seccomp="$LANE_SECCOMP_PROFILE" \
        --security-opt apparmor=unconfined \
        -e E2B_HOST_PROJECT="$LANE_HOST_PROJECT" \
        -e E2B_TEST_STRICT_SKIPS=1 \
        -e PYTHONUNBUFFERED=1 \
        -e E2B_TEST_ROOTFS_DIR="$LANE_ROOTFS_DIR" \
        -v "$HOME/.orbstack/run/docker.sock:/var/run/docker.sock" \
        -v "$LANE_HOST_PROJECT:/workspace" -w /workspace \
        "$image" \
        sleep infinity >/dev/null
}

lane_container_scratch() {
    # $1 = run id. A fresh scratch root per run, inside the reused container.
    echo "/var/lib/e2b-test-runtime/$1"
}

lane_container_reset_scratch() {
    # $1 = container name, $2 = run id. Non-destructive by construction: this
    # removes *this run's own* scratch directory (a directory nobody else has
    # ever written), and does nothing when the run never created one.
    scratch="$(lane_container_scratch "$2")"
    docker exec "$1" python3 -c '
import shutil, sys
shutil.rmtree(sys.argv[1], ignore_errors=True)
' "$scratch" >/dev/null 2>&1 || true
}

lane_container_warmup() {
    # $1 = container name. Absorb the one-time first-chroot-launch cost *here*,
    # under a verb deadline long enough for it (the default 15 s turns it into
    # two retries, and the pool's 30 s readiness deadline then kills whichever
    # test happened to be first). Failure is not fatal: a lane that cannot warm
    # up will simply pay the cost in the first test that needs a sandbox.
    docker exec \
        $LANE_EXTRA_EXEC_ENV \
        -e E2B_SLOT_READY_TIMEOUT_S="${LANE_WARMUP_READY_TIMEOUT_S:-180}" \
        "$1" python3 /workspace/deploy/scripts/lane-warmup.py ||
        echo "!! warm-up did not finish; the first sandbox test will pay the stall" >&2
}

lane_container_prepare_rootfs() {
    # $1 = container name. Hand an *unprivileged* phase the shared rootfs cache:
    # both the cache directory and its parent are created by the root phase's
    # process (same image, same `-e E2B_TEST_ROOTFS_DIR`), so uid 65534 fails
    # `resolve_test_rootfs` with `PermissionError: /var/lib/lane` -- and cannot
    # even fall back, because the fallback only fires on an *unset* value.
    # Measured 2026-10-11: 4 of phase 2's 60 tests failed on it, the rest
    # passed. uid 0 in this container needs no capability to mkdir or chmod, so
    # the `--cap-drop ALL` shape stays exactly what the phase is testing.
    docker exec -u 0 "$1" sh -c \
        "mkdir -p '$LANE_ROOTFS_DIR' && chmod 0777 '$LANE_ROOTFS_DIR' \
         && chmod a+rx \$(dirname '$LANE_ROOTFS_DIR')"
}

# --- the lane's services: two registries and one buildkitd, created once ----
#
# The fixtures start these per *pytest process* (a container create plus a
# readiness poll each: fast when the box is idle, tens of seconds when it is
# busy, up to 120 s in their failure paths). The lane runs two phases plus
# every iteration, so it creates them once, names them, and hands their
# addresses to every later process through the env the fixtures read
# (`tests/conftest.py`: `E2B_TEST_REGISTRY_URL`, `E2B_TEST_REGISTRY_AUTH_*`,
# `E2B_TEST_BUILDKIT_ADDR`). A named service that does not answer *fails* the
# fixture rather than skipping, which is those three's existing rule.
LANE_SERVICES_FILE="${LANE_SERVICES_FILE:-tmp/lane-services.env}"

lane_free_port() {
    python3 -c 'import socket
s = socket.socket(); s.bind(("127.0.0.1", 0))
print(s.getsockname()[1]); s.close()'
}

_lane_registry_port() {
    docker port "$1" 5000/tcp 2>/dev/null | head -1 | sed 's/.*://'
}

_lane_http_ready() {
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://$1/v2/" 2>/dev/null || true)"
    [ -n "$code" ] && echo "$code" | grep -Eq "^($2)$"
}

lane_services_env() {
    # The `-e` block for `docker exec`, read from the state file.
    [ -f "$LANE_SERVICES_FILE" ] || return 0
    # shellcheck disable=SC1090
    . "$LANE_SERVICES_FILE"
    printf '%s' "-e E2B_TEST_REGISTRY_URL=$LANE_REGISTRY_URL"
    printf ' %s' "-e E2B_TEST_REGISTRY_AUTH_URL=$LANE_REGISTRY_AUTH_URL"
    printf ' %s' "-e E2B_TEST_REGISTRY_AUTH_USER=$LANE_REGISTRY_AUTH_USER"
    printf ' %s' "-e E2B_TEST_REGISTRY_AUTH_PASSWORD=$LANE_REGISTRY_AUTH_PASSWORD"
    printf ' %s' "-e E2B_TEST_BUILDKIT_ADDR=$LANE_BUILDKIT_ADDR"
}

lane_services_up() {
    # $1 = the container to run the buildkit helper in (its python is the
    # suite's own fixture code, so the daemon is made the way pytest makes it).
    base="${LANE_CONTAINER:-e2b-prod-shaped-lane}"
    if [ -f "$LANE_SERVICES_FILE" ]; then
        # shellcheck disable=SC1090
        . "$LANE_SERVICES_FILE"
        reg_ok=0; auth_ok=0; buildkit_ok=0
        _lane_http_ready "$LANE_REGISTRY_URL" "200" && reg_ok=1
        _lane_http_ready "$LANE_REGISTRY_AUTH_URL" "200|401" && auth_ok=1
        [ -n "${LANE_BUILDKIT_ADDR:-}" ] && \
            nc -z 127.0.0.1 "${LANE_BUILDKIT_ADDR##*:}" 2>/dev/null && buildkit_ok=1
        if [ "$reg_ok" = 1 ] && [ "$auth_ok" = 1 ] && [ "$buildkit_ok" = 1 ]; then
            return 0
        fi
        echo "==> lane services incomplete (registry=$reg_ok auth=$auth_ok buildkit=$buildkit_ok): recreating" >&2
    fi

    docker rm -f "$base-registry" >/dev/null 2>&1 || true
    docker run -d --name "$base-registry" -p 127.0.0.1::5000 registry:2 >/dev/null
    LANE_REGISTRY_PORT="$(_lane_registry_port "$base-registry")"

    # htpasswd must be *in* the container before it starts (the registry reads
    # it at startup), so it is copied into a created-but-not-started container:
    # a bind mount would be resolved by the host daemon and can silently become
    # a directory (the same trap tests/conftest.py documents at length). The
    # copy is of a *directory* (`.../auth`, holding `htpasswd`) because plain
    # `docker cp file ctr:/auth/htpasswd` refuses a missing parent -- the same
    # reason the fixture ships the payload as a tar stream.
    LANE_REGISTRY_AUTH_USER="${LANE_REGISTRY_AUTH_USER:-testuser}"
    LANE_REGISTRY_AUTH_PASSWORD="${LANE_REGISTRY_AUTH_PASSWORD:-testpass}"
    # Under the repo's tmp/ (not $TMPDIR): `docker cp` reads it through the
    # *client's* view of the filesystem, and this is the one directory the
    # lanes already share with the daemon.
    mkdir -p tmp
    auth_dir="$(pwd)/tmp/lane-htpasswd.$$/auth"
    mkdir -p "$auth_dir"
    docker run --rm httpd:2-alpine htpasswd -Bbn \
        "$LANE_REGISTRY_AUTH_USER" "$LANE_REGISTRY_AUTH_PASSWORD" \
        >"$auth_dir/htpasswd"
    docker rm -f "$base-registry-auth" >/dev/null 2>&1 || true
    docker create --name "$base-registry-auth" -p 127.0.0.1::5000 \
        -e REGISTRY_AUTH=htpasswd \
        -e REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd \
        -e REGISTRY_AUTH_HTPASSWD_REALM=Registry \
        registry:2 >/dev/null
    docker cp "$(dirname "$auth_dir")/auth" "$base-registry-auth:/" >/dev/null
    docker start "$base-registry-auth" >/dev/null
    LANE_REGISTRY_AUTH_PORT="$(_lane_registry_port "$base-registry-auth")"

    buildkit_port="${LANE_BUILDKIT_PORT:-$(lane_free_port)}"
    LANE_BUILDKIT_ADDR="$(docker exec \
        $LANE_EXTRA_EXEC_ENV \
        -e E2B_BASE_IMAGE="${E2B_BASE_IMAGE:-python-mcp:3.14}" \
        -e E2B_TEST_BUILDKIT_NAME="$base-buildkit" \
        -e E2B_TEST_BUILDKIT_VOLUME="$base-buildkit-vol" \
        -e E2B_TEST_BUILDKIT_PORT="$buildkit_port" \
        "$1" python3 /workspace/deploy/scripts/lane-buildkitd.py | tail -1)"

    mkdir -p "$(dirname "$LANE_SERVICES_FILE")"
    cat >"$LANE_SERVICES_FILE" <<EOF
# Written by deploy/scripts/lib/lane-container.sh: the lane's long-lived
# services. Remove this file (or run with LANE_RECREATE=1) to rebuild them.
LANE_REGISTRY_URL=127.0.0.1:$LANE_REGISTRY_PORT
LANE_REGISTRY_AUTH_URL=127.0.0.1:$LANE_REGISTRY_AUTH_PORT
LANE_REGISTRY_AUTH_USER=$LANE_REGISTRY_AUTH_USER
LANE_REGISTRY_AUTH_PASSWORD=$LANE_REGISTRY_AUTH_PASSWORD
LANE_BUILDKIT_ADDR=$LANE_BUILDKIT_ADDR
EOF
    echo "==> lane services up: registry 127.0.0.1:$LANE_REGISTRY_PORT, auth registry 127.0.0.1:$LANE_REGISTRY_AUTH_PORT, buildkit $LANE_BUILDKIT_ADDR" >&2
}
