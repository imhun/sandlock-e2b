"""Shared fixtures: workspace, apps, live servers."""

from __future__ import annotations

import errno
import io
import os
import random
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path

import httpx
import pytest
import uvicorn

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.nodes import NodeRegistry
from envd_service.app import create_app as create_envd_app
from envd_service.app import PER_UID_NONROOT_WARNING
from envd_service.config import Settings as EnvdSettings
from envd_service.gateway import create_gateway
from envd_service.runtime.oci_registry import registry_mirrors
from envd_service.runtime.registry import RuntimeRegistry
from gateway_common.keepalive import uvicorn_keep_alive_kwargs
from control_plane.node_address import NodeEndpoint, StaticAddressResolver
from tests._c3_resolver import AnyNodeLoopbackResolver
from tests._disk_projids import DISK_READ_BACKENDS

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Ownership-sensitive tests (E3.2 per-sandbox uids) need a filesystem where
# chown works. On macOS hosts the repo bind mount is virtiofs (chown is a
# no-op there), so CI/local Linux runners keep the project-local default
# while Docker runners can point this at container-native storage.
TMP_ROOT = Path(
    os.environ.get(
        "E2B_TEST_TMP_ROOT", PROJECT_ROOT / "tmp" / "test-runtime"
    )
)

# Harness workspaces and shared volume roots live under TMP_ROOT as well, so a
# Docker runner puts them on container-native storage too: on the virtiofs bind
# mount of the repo, chown is a no-op, which silently voids every uid-ownership
# assertion (shared volume slices across distinct sandbox uids, for one).


# --- hermetic test network -------------------------------------------------
# ``httpx`` (and ``urllib``) fall back to the *system* proxy on macOS and to
# the Windows registry: a local Clash/PAC setup then silently intercepts every
# request, including the ones aimed at the ephemeral loopback ports these
# tests listen on. Concretely, a plaintext request against a TLS port came
# back as the proxy's own ``502`` instead of a handshake failure, and SDK
# log reads gained an extra hop. Nothing in this suite needs egress, so pin
# loopback to the no-proxy list before any client or server is constructed.
_LOCAL_NO_PROXY = ("127.0.0.1", "localhost", "::1")


def _keep_test_traffic_off_the_system_proxy() -> None:
    for key in ("NO_PROXY", "no_proxy"):
        entries = {e.strip() for e in os.environ.get(key, "").split(",") if e.strip()}
        entries.update(_LOCAL_NO_PROXY)
        os.environ[key] = ",".join(sorted(entries))


_keep_test_traffic_off_the_system_proxy()


def pytest_addoption(parser):
    parser.addoption("--perf", action="store_true", default=False, help="run perf tests")


# --- strict skip gate -------------------------------------------------------
# Every gate below is something the Docker test runner can genuinely satisfy
# (it ships xfsprogs + node/npm, loop-mounts XFS with prjquota at startup, and
# selects the image-rootfs / net-isolation shapes), so a skip that matches one
# of these reasons means coverage quietly disappeared. The docker daemon is in
# that set too: every lane mounts the socket in, so a fixture that skips
# because it cannot find `docker` is a mis-provisioned runner, not a narrower
# matrix -- that is the difference between "this environment cannot run
# containers at all" (an honest capability skip) and "the registry/buildkit
# container did not come up" (now a failure that quotes its log). With
# E2B_TEST_STRICT_SKIPS=1 (set in the test-runner image) such a skip is
# reported as a failure instead.
# Only *runner* capabilities are forbidden here -- XFS prjquota, node/npm,
# NET_ADMIN, the storage-ownership measurements -- because the image installs
# and prepares all of them, so a skip that names one of them means setup
# regressed. Deployment-shape selectors (E2B_BASE_IMAGE, E2B_TEST_NET_ISOLATION)
# are deliberately not in this list: switching them off is a supported way to
# run a narrower matrix, not lost coverage.
_STRICT_SKIP_FORBIDDEN = (
    "XFS quota integration requires",
    "does not support XFS project quota",
    "npm is not installed",
    "needs NET_ADMIN",
    "sandbox writes land owned by",
    "worker storage does not give the sandbox ownership",
    "docker is required for template build tests",
    "docker is required for the image registry tests",
    "docker is required for the authenticated registry tests",
)


def _strict_skips_enabled() -> bool:
    return os.environ.get("E2B_TEST_STRICT_SKIPS", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if not report.skipped or not _strict_skips_enabled():
        return report
    reason = str(report.longrepr or "")
    matched = next((marker for marker in _STRICT_SKIP_FORBIDDEN if marker in reason), None)
    if matched is None:
        return report
    report.outcome = "failed"
    report.longrepr = (
        f"{item.nodeid}: this runner is expected to satisfy the gate "
        f"(matched {matched!r} in E2B_TEST_STRICT_SKIPS mode), but the test "
        f"skipped with: {reason.strip()[:400]}"
    )
    return report


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--perf"):
        skip_perf = pytest.mark.skip(reason="perf tests require --perf")
        for item in items:
            if item.get_closest_marker("perf"):
                item.add_marker(skip_perf)


@pytest.fixture(params=DISK_READ_BACKENDS)
def disk_read_backend(request) -> str:
    """Which read backend the contract's project-id table answers through.

    ``directory_project_id`` has two of them (the fd backend and the
    ``lsattr`` fallback, in that order of preference) and W2 added the second
    stage that made a mount-only fake stop deciding anything on Linux, so the
    contracts that answer the read from an explicit table run once per form.
    See ``tests/_disk_projids.py`` for what the fakes do and why.
    """
    return request.param


def uid_startup_disclosure() -> list[str]:
    """The startup lines *this host's* worker emits before a create/delete.

    ``create_envd_app`` logs two independent decisions, in this order, and a
    contract that compares whole warning lists has to expect exactly the ones
    that fired here:

    * Track F's broker resolution: a non-root worker with
      ``E2B_PRIV_HELPERS=auto`` and no brokers installed says so once
      (``priv_helpers.helpers_unavailable_reason``); a root worker, ``off``,
      or an installed broker pair stays quiet;
    * E5.1's per-sandbox uids: without root and without the brokers the switch
      is auto-disabled with its own line; a root worker (or one that resolved
      the brokers) builds the uid pool and stays quiet.

    The suite runs in two shapes -- the root gate container (both silent) and
    an unprivileged dev box (the first line, then the second) -- so a helper
    that models only the second decision turns every exact-list assertion red
    on the dev box while the container stays green.
    """
    from envd_service import priv_helpers

    if os.geteuid() == 0 or priv_helpers.active_helpers() is not None:
        return []
    unavailable = priv_helpers.helpers_unavailable_reason(EnvdSettings())
    return [
        *([] if unavailable is None else [unavailable]),
        PER_UID_NONROOT_WARNING,
    ]


def _fresh_dir(path: Path) -> Path:
    """Start a test session from empty harness storage.

    The live/multinode fixtures keep their workspaces under ``tmp/`` between
    runs, which leaks state across sessions: a template record persisted with
    the registry port of an earlier run, for example, points at a registry that
    no longer exists. These directories are scratch data, so wipe them.
    """
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _warm_local_template_images(settings: ControlSettings) -> None:
    """Extract every image the local node can serve before the tests start.

    A create for a cold image fast-fails with 428 ``warm_required`` unless the
    caller opts into the slow path with ``X-Sandbox-Id``, and the official SDK
    does not send it. The multinode harness warms its remote workers the same
    way (``_warm_worker_base_image``); this covers the single-node shape.
    """
    from control_plane.api.sandboxes import _executor_needs_images

    if not _executor_needs_images(settings.executor):
        return
    from envd_service.runtime.image_resolver import resolve_image_rootfs

    images = {settings.base_image, *settings.template_images.values()} - {None, ""}
    for image in sorted(images):
        resolve_image_rootfs(
            image,
            settings.image_cache_dir,
            registry_username=settings.image_registry_username,
            registry_password=settings.image_registry_password,
            credential_host=settings.image_registry_host,
        )


# --- port handoff -----------------------------------------------------------
#: Ports the harness hands out come from *below* the kernel's ephemeral range
#: (Linux: 32768-60999) and below the worker's MCP-gateway port pool base
#: (51000, ``envd_service/runtime/context.py``). Probing with
#: ``bind(("127.0.0.1", 0))`` instead draws from the ephemeral range -- the same
#: range every loopback connection in the suite takes its *source* port from,
#: with thousands of them per lane -- so a port a probe just found free can be
#: claimed by an outgoing connection (or by the MCP pool handing the same
#: number to a sandbox gateway) before the server binds it. Observed as
#: ``_ServerThread.start`` -> ``[Errno 98] address already in use``.
_PORT_POOL_MIN = 20000
_PORT_POOL_MAX = 28000
_PORT_PICK_ATTEMPTS = 64


def _bind_low_port() -> tuple[int, socket.socket]:
    """Bind *and* listen on a free harness port, and keep the socket.

    The returned socket is the one a server must serve on
    (``_ServerThread(..., sock=sock)``): the port stays bound from the moment
    it is chosen, so nothing can take it between "found free" and "server
    bound it". That window is exactly what the old probe/close/rebind shape
    left open, and closing it is the fix -- narrowing it with a retry would
    keep the race, only rarer.
    """
    for _ in range(_PORT_PICK_ATTEMPTS):
        candidate = random.randrange(_PORT_POOL_MIN, _PORT_POOL_MAX)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", candidate))
        except OSError as exc:
            sock.close()
            if exc.errno == errno.EADDRINUSE:
                continue
            raise
        sock.listen(128)
        return candidate, sock
    raise RuntimeError(
        f"no free port in the harness pool ({_PORT_POOL_MIN}-{_PORT_POOL_MAX})"
    )


def _free_container_port() -> int:
    """A harness-pool port for a *container* to bind itself (buildkitd).

    A container binds its port itself, so the port cannot be handed over as a
    socket; this is the one place the probe/close shape survives. The pool
    range is what keeps it safe: a collision now needs another *listener* on
    that port, never an outgoing connection's ephemeral source port, and the
    callers that use it verify the container actually listens (loudly) instead
    of trusting the probe.
    """
    port, sock = _bind_low_port()
    sock.close()
    return port


def _port_accepting(port: int) -> bool:
    """True when something is listening on ``127.0.0.1:port`` right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def _docker_container(
    name: str,
    args: list[str],
    *,
    files: dict[str, Path] | None = None,
) -> str:
    """Create a container, inject ``files`` (container path -> local file), then
    start it; returns the container id.

    ``docker cp`` moves the bytes through the daemon API while the container is
    stopped, so a file a process needs at startup never depends on the daemon
    resolving a *host* path. The bind-mount shape this replaces is resolved by
    the daemon, which turns a source it cannot see into an empty directory
    (see ``_start_buildkitd``). The payload travels as a tar stream
    (``docker cp - CONTAINER:/``) because plain ``docker cp src ctr:/a/b/c``
    refuses a missing parent directory -- the tar creates the whole path, so a
    file can land at the process's own default location.
    """
    created = subprocess.run(
        ["docker", "create", "--name", name, *args],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        raise RuntimeError(created.stderr.strip() or "docker create failed")
    container = created.stdout.strip()
    try:
        if files:
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as tar:
                for dest, src in files.items():
                    data = src.read_bytes()
                    entry = tarfile.TarInfo(name=dest.lstrip("/"))
                    entry.size = len(data)
                    entry.mode = 0o644
                    tar.addfile(entry, io.BytesIO(data))
            payload.seek(0)
            copied = subprocess.run(
                ["docker", "cp", "-", f"{container}:/"],
                input=payload.read(),
                capture_output=True,
            )
            if copied.returncode != 0:
                raise RuntimeError(
                    "docker cp - -> / failed: "
                    f"{copied.stderr.decode(errors='replace').strip()}"
                )
        started = subprocess.run(
            ["docker", "start", container], capture_output=True, text=True
        )
        if started.returncode != 0:
            raise RuntimeError(f"docker start failed: {started.stderr.strip()}")
    except BaseException:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)
        raise
    return container


def _published_port(container: str, container_port: int) -> int:
    """Host port docker assigned for ``-p 127.0.0.1::<container_port>``.

    Docker picks the host port at bind time and the harness reads it back, so
    the registry fixtures never probe a port and re-bind it later: the
    ephemeral-range race cannot happen at all (the "let the binder pick port 0
    and read it back" shape).
    """
    published = subprocess.run(
        ["docker", "port", container, str(container_port)],
        capture_output=True,
        text=True,
    )
    stdout = published.stdout.strip()
    if published.returncode != 0 or not stdout:
        raise RuntimeError(
            f"container published no port {container_port}: {published.stderr.strip()}"
        )
    return int(stdout.rsplit(":", 1)[1])


def _buildkit_mirror_urls() -> list[str]:
    """The docker.io mirrors buildkitd should be configured with.

    Parsed by the resolver's own ``registry_mirrors`` so the daemon and
    ``envd_service.runtime.oci_registry`` can never end up on different
    sources: a mirror that refuses buildkit pulls while the resolver is fine
    (or the reverse) is exactly the same-source flake this fixture used to
    cause by hardcoding one mirror. No env at all -> the resolver's default
    chain; explicitly empty -> no mirrors (pull the origin directly).
    Mirror hosts get the resolver's own scheme rule: https://, except loopback
    (plain HTTP, the local test registry). An explicit ``http://`` on a
    non-loopback host is normalized away by the same rule, exactly as the
    resolver does it -- the two must agree or the fixture reintroduces drift.
    """
    buckets = registry_mirrors()
    mirrors: list[str] = []
    for alias in ("registry-1.docker.io", "docker.io", "index.docker.io"):
        if buckets.get(alias):
            mirrors = list(buckets[alias])
            break
    urls: list[str] = []
    for mirror in mirrors:
        if mirror.startswith(("http://", "https://")):
            urls.append(mirror)
            continue
        host = mirror.split(":")[0]
        scheme = "http" if host in ("127.0.0.1", "localhost") else "https"
        urls.append(f"{scheme}://{mirror}")
    return urls


#: Where buildkitd looks for its config inside the rootless image (the image's
#: own user's default XDG path). The file is injected with ``docker cp`` -- see
#: ``_start_buildkitd`` for why it is not a bind mount.
_BUILDKIT_CONFIG_PATH = "/home/user/.config/buildkit/buildkitd.toml"


def _buildkit_config_text(port: int | str) -> str:
    """buildkitd config: TCP listener + the resolver's docker.io mirrors.

    Public-image pulls go through the same bucket the envd resolver uses
    (``E2B_REGISTRY_MIRRORS``); one parsing function, so a hardcoded single
    source cannot drift back in (see ``_buildkit_mirror_urls``).
    """
    mirror_entries = ", ".join(f'"{m}"' for m in _buildkit_mirror_urls())
    return (
        f'[grpc]\n  address = ["tcp://0.0.0.0:{port}"]\n\n'
        "[worker.oci]\n  noProcessSandbox = true\n\n"
        f'[registry."docker.io"]\n  mirrors = [{mirror_entries}]\n\n'
        # Local test registry is plain HTTP on 127.0.0.1 (any port).
        '[registry."127.0.0.1"]\n  http = true\n'
    )


def _buildkitd_failure(container: str, detail: str) -> str:
    """Failure text for the buildkit fixture: what we expected + its own log."""
    logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
    tail = f"{logs.stdout}{logs.stderr}".strip()[-2000:]
    return f"buildkitd {detail}\n--- buildkitd log tail ---\n{tail}"


def _stop_buildkitd(container: str, cfg_dir: Path) -> None:
    subprocess.run(["docker", "rm", "-f", container], capture_output=True)
    shutil.rmtree(cfg_dir, ignore_errors=True)


def _start_buildkitd() -> tuple[str, str, Path]:
    """Start the rootless buildkit daemon; ``(tcp address, container, cfg_dir)``.

    The config is *injected* into the created-but-not-started container with
    ``docker cp`` instead of being bind-mounted. A bind mount is resolved by
    the HOST daemon, so a file the test container just wrote (through its
    ``/workspace`` view of the tree) -- or any run whose tree path differs from
    ``E2B_HOST_PROJECT``, e.g. the ``git archive`` snapshots the lanes are
    reproduced on -- is a source the daemon cannot see: it creates an empty
    *directory* in its place and buildkitd dies on
    ``read .../buildkitd.toml: is a directory`` (observed twice: a 24-test skip
    in ``tests/contract`` and 9 failures in ``tests/sdk``). ``docker cp``
    carries the bytes through the daemon API, so no host path is resolved at
    all, and the two checks below turn any recurrence into a loud failure that
    quotes buildkitd's own log (never a silent skip that drops coverage).
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is required for template build tests")
    port = _free_container_port()
    config_text = _buildkit_config_text(port)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    cfg_dir = Path(tempfile.mkdtemp(prefix="buildkit-cfg-", dir=TMP_ROOT))
    cfg = cfg_dir / "buildkitd.toml"
    cfg.write_text(config_text, encoding="utf-8")
    name = f"buildkit-test-{uuid.uuid4().hex[:8]}"
    volume = f"buildkit-test-vol-{uuid.uuid4().hex[:8]}"
    container = ""
    try:
        try:
            container = _docker_container(
                name,
                [
                    # No --rm: the teardown removes the container, and keeping
                    # it in `docker ps -a` is what makes the failure report
                    # able to quote buildkitd's own log.
                    "--security-opt",
                    "seccomp=unconfined",
                    "--security-opt",
                    "label=disable",
                    "--network",
                    "host",
                    "-v",
                    f"{volume}:/home/user/.local/share/buildkit",
                    "moby/buildkit:rootless",
                ],
                files={_BUILDKIT_CONFIG_PATH: cfg},
            )
        except RuntimeError as exc:
            # Docker itself is unusable (no daemon/permission): the same class
            # as "docker is required" above.
            pytest.skip(f"cannot start buildkit container: {exc}")

        # Readiness is "listening on the address this config asked for", which
        # only happens if the daemon parsed *this* file -- not "the container
        # process is still alive" (a daemon that dies on a bad config, or one
        # running with defaults after a failed config load, is not serving).
        deadline = time.time() + 90
        while time.time() < deadline and not _port_accepting(port):
            time.sleep(0.25)
        if not _port_accepting(port):
            pytest.fail(
                _buildkitd_failure(
                    container,
                    f"did not listen on 127.0.0.1:{port} within 90s",
                ),
                pytrace=False,
            )
        read_back = subprocess.run(
            ["docker", "exec", container, "cat", _BUILDKIT_CONFIG_PATH],
            capture_output=True,
            text=True,
        )
        if read_back.returncode != 0 or read_back.stdout != config_text:
            pytest.fail(
                _buildkitd_failure(
                    container,
                    f"{_BUILDKIT_CONFIG_PATH} is not the config file we copied "
                    f"(exit={read_back.returncode}, stderr="
                    f"{read_back.stderr.strip()!r}, bytes={len(read_back.stdout)})",
                ),
                pytrace=False,
            )
    except BaseException:
        if container:
            _stop_buildkitd(container, cfg_dir)
        else:
            shutil.rmtree(cfg_dir, ignore_errors=True)
        raise
    return f"tcp://127.0.0.1:{port}", container, cfg_dir


@pytest.fixture(scope="session")
def buildkitd():
    """Rootless buildkit daemon (TCP) for local template builds."""
    address, container, cfg_dir = _start_buildkitd()
    try:
        yield address
    finally:
        _stop_buildkitd(container, cfg_dir)


@pytest.fixture()
def workspace(tmp_path) -> Path:
    """Project-local workspace for one test (spec: temp data in tmp/)."""
    name = f"{uuid.uuid4().hex[:12]}"
    path = TMP_ROOT / name
    path.mkdir(parents=True, exist_ok=True)
    yield path


@pytest.fixture()
def apps(workspace):
    """Control plane + envd apps sharing a runtime registry (in-process)."""
    runtime_registry = RuntimeRegistry(workspace)
    control_settings = ControlSettings(
        api_keys=("local-key",),
        # E9.4: keep the shared fixture on the pre-queue behavior (immediate
        # 503 on full pools); queue tests opt in via make_apps(...).
        create_queue_timeout_s=0,
    )
    envd_settings = EnvdSettings(executor="local")
    control_app = create_control_app(
        settings=control_settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
        # C3 Task 2: every "node" in this lane is the in-process client, so the
        # internal API's expected-address resolver answers loopback (production
        # reads the k8s pod API / compose DNS).
        node_address_resolver=AnyNodeLoopbackResolver(),
    )
    envd_app = create_envd_app(
        settings=envd_settings,
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    return control_app, envd_app


@pytest.fixture()
def make_apps(workspace):
    """Factory for apps with custom control-plane settings."""

    def _make(*, control_settings=None, envd_settings=None, control_kwargs=None):
        runtime_registry = RuntimeRegistry(workspace)
        control = create_control_app(
            settings=control_settings
            or ControlSettings(
                api_keys=("local-key",),
                create_queue_timeout_s=0,
            ),
            runtime_registry=runtime_registry,
            workspace_base=workspace,
            node_address_resolver=AnyNodeLoopbackResolver(),
            **(control_kwargs or {}),
        )
        envd = create_envd_app(
            settings=envd_settings or EnvdSettings(executor="local"),
            runtime_registry=runtime_registry,
            workspace_base=workspace,
        )
        return control, envd

    return _make


@pytest.fixture()
async def control_client(apps):
    app, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture()
async def envd_client(apps):
    _, app = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


class _ServerThread:
    def __init__(
        self,
        app,
        port: int,
        *,
        sock: socket.socket | None = None,
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
    ) -> None:
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
            ssl_certfile=ssl_certfile,
            ssl_keyfile=ssl_keyfile,
            # Keep an idle connection open past the SDK's own pool idle window
            # (``gateway_common.keepalive``): the client reuses a pooled
            # connection for a bidi ``process.Process/Start`` whose body it
            # cannot replay, so a server that closes an idle connection first
            # costs that RPC a reset. Same knobs as every service entry point.
            **uvicorn_keep_alive_kwargs(),
        )
        self.server = uvicorn.Server(config)
        # Serve the caller's *reserved* socket (``_bind_low_port``) when there
        # is one: uvicorn then adopts the already-bound port instead of binding
        # it a second time, which is what removes the free-port race window
        # rather than narrowing it.
        self.sockets = [sock] if sock is not None else None
        self.thread = threading.Thread(
            target=lambda: self.server.run(self.sockets), daemon=True
        )

    def start(self) -> None:
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server failed to start")
            time.sleep(0.05)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        for sock in self.sockets or []:
            try:
                sock.close()
            except OSError:
                pass


@pytest.fixture(scope="session")
def live_servers(buildkitd):
    """Real uvicorn servers for the official SDK tests."""
    remote = os.environ.get("E2B_TEST_PROXY_URL")
    if remote:
        # Point the SDK suite at a deployed instance (e.g. through the SLB
        # proxy) instead of starting local servers.
        url = remote.rstrip("/")
        os.environ["E2B_API_URL"] = url
        os.environ["E2B_SANDBOX_URL"] = url
        os.environ["E2B_VOLUME_API_URL"] = url
        # E2B_API_KEY / E2B_INTERNAL_API_KEY come from the environment.
        yield {"api_url": url, "sandbox_url": url}
        return
    sdk_workspace = _fresh_dir(TMP_ROOT / "sdk-workspace")
    runtime_registry = RuntimeRegistry(sdk_workspace)
    control_port, control_sock = _bind_low_port()
    envd_port, envd_sock = _bind_low_port()
    control_app = create_control_app(
        settings=ControlSettings(
            api_keys=("local-key",),
            control_plane_port=control_port,
            envd_port=envd_port,
            template_images={"py311": "python:3.11-slim"},
            max_sandboxes=500,
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
            # The suite creates far more sandboxes per minute than a
            # production budget allows (120/min by default), which shows up
            # as 429 in SDK fixtures; the limiter itself has its own tests.
            create_rate_limit_per_min=0,
            # The resource-creating endpoints share that reasoning: the
            # suite creates far more volumes/snapshots per minute than a
            # production budget allows.
            snapshot_rate_limit_per_min=0,
            volume_rate_limit_per_min=0,
            buildkit_addr=buildkitd,
        ),
        runtime_registry=runtime_registry,
        workspace_base=sdk_workspace,
    )
    envd_app = create_envd_app(
        settings=EnvdSettings(
            executor="local",
            envd_port=envd_port,
        ),
        runtime_registry=runtime_registry,
        workspace_base=sdk_workspace,
    )
    _warm_local_template_images(control_app.state.settings)
    control = _ServerThread(control_app, control_port, sock=control_sock)
    envd = _ServerThread(envd_app, envd_port, sock=envd_sock)
    control.start()
    envd.start()

    old = {
        "api_url": os.environ.get("E2B_API_URL"),
        "sandbox_url": os.environ.get("E2B_SANDBOX_URL"),
        "api_key": os.environ.get("E2B_API_KEY"),
        "volume_api_url": os.environ.get("E2B_VOLUME_API_URL"),
    }
    os.environ["E2B_API_URL"] = f"http://127.0.0.1:{control_port}"
    os.environ["E2B_SANDBOX_URL"] = f"http://127.0.0.1:{envd_port}"
    os.environ["E2B_API_KEY"] = "local-key"
    os.environ["E2B_VOLUME_API_URL"] = f"http://127.0.0.1:{control_port}"

    yield {
        "api_url": f"http://127.0.0.1:{control_port}",
        "sandbox_url": f"http://127.0.0.1:{envd_port}",
    }

    for key, value in old.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    envd.stop()
    control.stop()


@pytest.fixture(scope="session")
def multinode_servers(buildkitd):
    """Real control plane + one remote worker + envd gateway."""
    harness = _start_multinode(
        TMP_ROOT / "multinode", 1, buildkit_addr=buildkitd
    )
    yield {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "worker_url": harness["worker_urls"][0],
        "nodes": harness["nodes"],
    }
    harness["_stop"]()


def _start_multinode(
    root: Path,
    worker_count: int,
    *,
    shared_workspace: bool = False,
    warm_base_image: bool = False,
    image_registry: str | None = None,
    image_registry_username: str | None = None,
    image_registry_password: str | None = None,
    buildkit_addr: str | None = None,
    envd_settings_extra: dict | None = None,
) -> dict:
    """Shared harness: control plane + N workers + envd gateway."""
    _fresh_dir(root)
    control_dir = root / "control"
    shared_volumes = root / "shared-volumes"
    shared_workspace_dir = (
        root / "shared-workspace" if shared_workspace else control_dir
    )
    control_dir.mkdir(parents=True, exist_ok=True)
    shared_volumes.mkdir(parents=True, exist_ok=True)
    if shared_workspace:
        shared_workspace_dir.mkdir(parents=True, exist_ok=True)

    control_port, control_sock = _bind_low_port()
    gateway_port, gateway_sock = _bind_low_port()
    worker_sockets = [_bind_low_port() for _ in range(worker_count)]
    worker_ports = [port for port, _sock in worker_sockets]

    nodes = NodeRegistry(heartbeat_timeout=12)
    # C3 Task 2 (D4/D5): the internal API derives each worker's expected
    # address/IP from a resolver, never from the registration body, and refuses
    # a node-scoped request whose claim does not resolve. The harness declares
    # stable node ids (``worker-1``…) and hands the control plane their
    # loopback endpoints, the same injection production gets from the k8s pod
    # API / compose DNS.
    worker_node_ids = [f"worker-{index + 1}" for index in range(worker_count)]
    node_endpoints = {
        node_id: NodeEndpoint(f"http://127.0.0.1:{port}", "127.0.0.1")
        for node_id, port in zip(worker_node_ids, worker_ports)
    }
    control_app = create_control_app(
        settings=ControlSettings(
            api_keys=("local-key",),
            control_plane_port=control_port,
            envd_port=worker_ports[0],
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
            # The suite creates far more sandboxes per minute than a
            # production budget allows (120/min by default), which shows up
            # as 429 in SDK fixtures; the limiter itself has its own tests.
            create_rate_limit_per_min=0,
            # The resource-creating endpoints share that reasoning: the
            # suite creates far more volumes/snapshots per minute than a
            # production budget allows.
            snapshot_rate_limit_per_min=0,
            volume_rate_limit_per_min=0,
            workspace_base=shared_workspace_dir,
            shared_workspace_root=(
                str(shared_workspace_dir) if shared_workspace else None
            ),
            image_registry=image_registry,
            image_registry_username=image_registry_username,
            image_registry_password=image_registry_password,
            shared_volume_root=str(shared_volumes),
            gateway_url=f"http://127.0.0.1:{gateway_port}",
            buildkit_addr=buildkit_addr,
        ),
        runtime_registry=RuntimeRegistry(shared_workspace_dir),
        workspace_base=shared_workspace_dir,
        nodes_registry=nodes,
        node_address_resolver=StaticAddressResolver(node_endpoints),
    )
    # Force scheduling onto the registered remote workers.
    control_app.state.nodes.remove("local")

    worker_apps = []
    worker_dirs = []
    envd_extra = dict(envd_settings_extra or {})
    for index, worker_port in enumerate(worker_ports):
        worker_dir = root / f"worker-{index + 1}"
        worker_dir.mkdir(parents=True, exist_ok=True)
        worker_dirs.append(worker_dir)
        worker_base = shared_workspace_dir if shared_workspace else worker_dir
        worker_apps.append(
            create_envd_app(
                settings=EnvdSettings(
                    executor="auto",
                    envd_port=worker_port,
                    workspace_base=worker_base,
                    # The E2B network API is per-sandbox: the platform-level
                    # switch must be on so sandbox-level allow/deny policies
                    # are enforced (default off denies all egress).
                    enable_network=True,
                    shared_volume_root=str(shared_volumes),
                    # The worker needs the registry host too: the pull
                    # credentials are scoped to it (public images stay
                    # anonymous).
                    image_registry=image_registry,
                    image_registry_username=image_registry_username,
                    image_registry_password=image_registry_password,
                    **envd_extra,
                ),
                runtime_registry=RuntimeRegistry(worker_base),
                workspace_base=worker_base,
                control_plane_url=f"http://127.0.0.1:{control_port}",
                node_address=f"http://127.0.0.1:{worker_port}",
                node_id=worker_node_ids[index],
            )
        )
    gateway_app = create_gateway(
        control_plane_url=f"http://127.0.0.1:{control_port}",
        internal_api_key="internal-key",
    )

    control = _ServerThread(control_app, control_port, sock=control_sock)
    workers = [
        _ServerThread(app, port, sock=sock)
        for app, (port, sock) in zip(worker_apps, worker_sockets)
    ]
    gateway = _ServerThread(gateway_app, gateway_port, sock=gateway_sock)
    control.start()
    for worker in workers:
        worker.start()
    gateway.start()

    # Wait for every worker agent to register with the control plane.
    deadline = time.time() + 30
    while time.time() < deadline:
        registered = [
            n for n in nodes.list() if n.address != "local://"
        ]
        if len(registered) >= worker_count:
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("worker nodes did not register with the control plane")

    if warm_base_image:
        base_image = control_app.state.settings.base_image
        if base_image:
            for worker_url in (f"http://127.0.0.1:{p}" for p in worker_ports):
                _warm_worker_base_image(
                    worker_url,
                    base_image,
                    control_app.state.settings.internal_api_key,
                )

    return {
        "api_url": f"http://127.0.0.1:{control_port}",
        "sandbox_url": f"http://127.0.0.1:{gateway_port}",
        "worker_urls": [f"http://127.0.0.1:{p}" for p in worker_ports],
        # The envd apps themselves: contracts that must compare the SDK-visible
        # result with worker-side runtime state (e.g. the FUP #4 gateway
        # failure record) read it off ``app.state.runtimes``.
        "worker_apps": worker_apps,
        "nodes": nodes,
        "control_app": control_app,
        "_stop": lambda: (
            gateway.stop(),
            [w.stop() for w in workers],
            control.stop(),
        ),
    }


def _warm_worker_base_image(
    worker_url: str, image: str, internal_api_key: str
) -> None:
    """Ensure ``image`` is extracted on a worker (idempotent peek + warm).

    Security e2e tests create base-image sandboxes through the SDK without
    the ``X-Sandbox-Id`` header; on a cold image cache the control plane
    answers 428 ``warm_required``. Warming every worker up front makes the
    harness deterministic regardless of the local image cache state.
    """
    from urllib.parse import quote

    import httpx

    url = f"{worker_url}/agent/images/{quote(image, safe='/:')}/warm"
    headers = {"X-Internal-Key": internal_api_key}
    with httpx.Client(timeout=30) as client:
        peek = client.get(url, headers=headers)
        if peek.status_code == 200 and peek.json().get("cached"):
            return
        warm = client.post(url, headers=headers)
        if warm.status_code != 200:
            raise RuntimeError(
                f"worker {worker_url} failed to warm base image {image}: "
                f"{warm.status_code} {warm.text[:200]}"
            )


@pytest.fixture(scope="session")
def multinode_two_workers(buildkitd):
    """Real control plane + two remote workers + envd gateway."""
    harness = _start_multinode(
        TMP_ROOT / "multinode-two",
        2,
        buildkit_addr=buildkitd,
        warm_base_image=True,
    )
    yield harness
    harness["_stop"]()


@pytest.fixture(scope="session")
def multinode_shared_workspace(buildkitd):
    """Two workers sharing one E2B_WORKSPACE_BASE (NFS-style shared storage)."""
    harness = _start_multinode(
        TMP_ROOT / "multinode-shared",
        2,
        shared_workspace=True,
        buildkit_addr=buildkitd,
    )
    yield harness
    harness["_stop"]()


def _registry_failure(container: str, detail: str) -> str:
    """Failure text for a registry fixture: what we expected + its own log.

    Same rule as ``_buildkitd_failure``: a registry that does not come up is a
    *regression* of the fixture (or of the docker it runs on), not a reason to
    drop the registry-backed coverage silently. The container is still there
    while the fixture waits -- ``--rm`` only removes it after it exits -- so
    ``docker logs`` can quote the registry's own output here.
    """
    logs = subprocess.run(["docker", "logs", container], capture_output=True, text=True)
    tail = f"{logs.stdout}{logs.stderr}".strip()[-2000:]
    return f"registry {detail}\n--- registry log tail ---\n{tail}"


@pytest.fixture(scope="session")
def image_registry_url():
    """A disposable Docker registry (``registry:2``) on a docker-assigned port.

    Every step after the docker-capability check *fails* -- it never skips.
    A registry that cannot be started, published or reached means the
    template-push contracts lost their subject; skipping there is exactly the
    silent-coverage-loss this fixture used to do (the same shape the buildkitd
    fixture had). The one skip left is ``docker`` itself being absent from the
    machine, which ``_STRICT_SKIP_FORBIDDEN`` also turns into a failure inside
    the gate (every lane runs with the daemon socket mounted).
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is required for the image registry tests")
    start = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            # ``-p 127.0.0.1::5000`` lets docker pick the host port when it
            # binds and the harness read it back (``_published_port``): no
            # port is probed and then rebound later, so nothing can take it in
            # between.
            "-p",
            "127.0.0.1::5000",
            "registry:2",
        ],
        capture_output=True,
        text=True,
    )
    if start.returncode != 0:
        pytest.fail(
            f"cannot start registry container: {start.stderr.strip()}",
            pytrace=False,
        )
    container_id = start.stdout.strip()
    try:
        port = _published_port(container_id, 5000)
    except RuntimeError as exc:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        pytest.fail(f"cannot read the registry's published port: {exc}", pytrace=False)
    deadline = time.time() + 120
    ready = False
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/v2/", timeout=2)
            if resp.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    if not ready:
        failure = _registry_failure(
            container_id, f"did not answer 200 on 127.0.0.1:{port}/v2/ within 120s"
        )
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        pytest.fail(failure, pytrace=False)
    yield f"127.0.0.1:{port}"
    subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


@pytest.fixture(scope="session")
def authenticated_registry():
    """A Docker registry requiring basic auth (bcrypt htpasswd).

    Same rule as ``image_registry_url``: after the docker-capability check
    every failure fails the run with the container's own log instead of
    skipping.
    """
    if shutil.which("docker") is None:
        pytest.skip("docker is required for the authenticated registry tests")
    username, password = "testuser", "testpass"
    ht = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "httpd:2-alpine",
            "htpasswd",
            "-Bbn",
            username,
            password,
        ],
        capture_output=True,
        text=True,
    )
    if ht.returncode != 0:
        pytest.fail(f"cannot generate htpasswd: {ht.stderr.strip()}", pytrace=False)
    # The registry reads htpasswd at startup, so the file is injected into the
    # created-but-not-started container with ``docker cp`` (same rule as
    # buildkitd's config): a bind mount is resolved by the daemon on the HOST,
    # and a source it cannot see becomes an empty *directory* -- the registry
    # then mounts a directory as its htpasswd file and answers 400 on login.
    # ``docker cp`` resolves no host path at all, so there is nothing to race.
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="registry-auth-", dir=TMP_ROOT
        ) as tmp:
            htpasswd_path = Path(tmp) / "htpasswd"
            htpasswd_path.write_text(ht.stdout, encoding="utf-8")
            container_id = _docker_container(
                f"registry-auth-{uuid.uuid4().hex[:8]}",
                [
                    "-p",
                    "127.0.0.1::5000",
                    "-e",
                    "REGISTRY_AUTH=htpasswd",
                    "-e",
                    "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd",
                    "-e",
                    "REGISTRY_AUTH_HTPASSWD_REALM=Registry",
                    "registry:2",
                ],
                files={"/auth/htpasswd": htpasswd_path},
            )
    except RuntimeError as exc:
        pytest.fail(f"cannot start authenticated registry: {exc}", pytrace=False)
    try:
        port = _published_port(container_id, 5000)
    except RuntimeError as exc:
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        pytest.fail(f"cannot read the registry's published port: {exc}", pytrace=False)
    deadline = time.time() + 120
    ready = False
    while time.time() < deadline:
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/v2/", timeout=2)
            if resp.status_code in (200, 401):
                ready = True
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    if not ready:
        failure = _registry_failure(
            container_id,
            f"did not answer 200/401 on 127.0.0.1:{port}/v2/ within 120s",
        )
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        pytest.fail(failure, pytrace=False)
    yield {
        "url": f"127.0.0.1:{port}",
        "username": username,
        "password": password,
    }
    subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


def _start_live_servers(
    name: str,
    *,
    image_registry: str | None = None,
    image_registry_username: str | None = None,
    image_registry_password: str | None = None,
    buildkit_addr: str | None = None,
) -> dict:
    """Real control plane + envd servers for registry template builds."""
    workspace = _fresh_dir(TMP_ROOT / name)
    runtime_registry = RuntimeRegistry(workspace)
    control_port, control_sock = _bind_low_port()
    envd_port, envd_sock = _bind_low_port()
    control_app = create_control_app(
        settings=ControlSettings(
            api_keys=("local-key",),
            control_plane_port=control_port,
            envd_port=envd_port,
            image_registry=image_registry,
            image_registry_username=image_registry_username,
            image_registry_password=image_registry_password,
            buildkit_addr=buildkit_addr,
            max_sandboxes=500,
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
            # The suite creates far more sandboxes per minute than a
            # production budget allows (120/min by default), which shows up
            # as 429 in SDK fixtures; the limiter itself has its own tests.
            create_rate_limit_per_min=0,
            # The resource-creating endpoints share that reasoning: the
            # suite creates far more volumes/snapshots per minute than a
            # production budget allows.
            snapshot_rate_limit_per_min=0,
            volume_rate_limit_per_min=0,
        ),
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    envd_app = create_envd_app(
        settings=EnvdSettings(
            executor="local",
            envd_port=envd_port,
        ),
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    _warm_local_template_images(control_app.state.settings)
    control = _ServerThread(control_app, control_port, sock=control_sock)
    envd = _ServerThread(envd_app, envd_port, sock=envd_sock)
    control.start()
    envd.start()
    return {
        "api_url": f"http://127.0.0.1:{control_port}",
        "sandbox_url": f"http://127.0.0.1:{envd_port}",
        "image_registry": image_registry,
        "_stop": lambda: (envd.stop(), control.stop()),
    }


@pytest.fixture(scope="session")
def live_servers_registry(buildkitd, image_registry_url):
    """Real servers whose template builds push to a local Docker registry."""
    harness = _start_live_servers(
        "sdk-workspace-registry",
        image_registry=image_registry_url,
        buildkit_addr=buildkitd,
    )
    yield harness
    harness["_stop"]()


@pytest.fixture(scope="session")
def live_servers_registry_auth(buildkitd, authenticated_registry):
    """Real servers pushing to a registry that requires basic auth."""
    harness = _start_live_servers(
        "sdk-workspace-registry-auth",
        image_registry=authenticated_registry["url"],
        image_registry_username=authenticated_registry["username"],
        image_registry_password=authenticated_registry["password"],
        buildkit_addr=buildkitd,
    )
    yield {**harness, "auth": authenticated_registry}
    harness["_stop"]()


@pytest.fixture(scope="session")
def multinode_servers_registry(buildkitd, image_registry_url):
    """Single worker + control plane that pushes templates to a registry."""
    harness = _start_multinode(
        TMP_ROOT / "multinode-registry",
        1,
        image_registry=image_registry_url,
        buildkit_addr=buildkitd,
    )
    yield {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "worker_url": harness["worker_urls"][0],
        "nodes": harness["nodes"],
        "image_registry": image_registry_url,
    }
    harness["_stop"]()


@pytest.fixture(scope="session")
def multinode_servers_registry_auth(buildkitd, authenticated_registry):
    """Single worker + control plane pushing to an authenticated registry."""
    harness = _start_multinode(
        TMP_ROOT / "multinode-registry-auth",
        1,
        image_registry=authenticated_registry["url"],
        image_registry_username=authenticated_registry["username"],
        image_registry_password=authenticated_registry["password"],
        buildkit_addr=buildkitd,
    )
    yield {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "worker_url": harness["worker_urls"][0],
        "nodes": harness["nodes"],
        "image_registry": authenticated_registry["url"],
        "auth": authenticated_registry,
    }
    harness["_stop"]()
