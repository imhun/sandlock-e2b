"""Shared fixtures: workspace, apps, live servers."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
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
from envd_service.config import Settings as EnvdSettings
from envd_service.gateway import create_gateway
from envd_service.runtime.registry import RuntimeRegistry

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


def pytest_addoption(parser):
    parser.addoption("--perf", action="store_true", default=False, help="run perf tests")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--perf"):
        skip_perf = pytest.mark.skip(reason="perf tests require --perf")
        for item in items:
            if item.get_closest_marker("perf"):
                item.add_marker(skip_perf)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def buildkitd():
    """Rootless buildkit daemon (TCP) for local template builds."""
    if shutil.which("docker") is None:
        pytest.skip("docker is required for template build tests")
    port = _free_port()
    import tempfile

    cfg_dir = Path(tempfile.mkdtemp(prefix="buildkit-test-"))
    cfg = cfg_dir / "buildkitd.toml"
    cfg.write_text(
        f'[grpc]\n  address = ["tcp://0.0.0.0:{port}"]\n\n'
        "[worker.oci]\n  noProcessSandbox = true\n\n"
        '[registry."docker.io"]\n  mirrors = ["https://docker.m.daocloud.io"]\n\n'
        # Local test registry is plain HTTP on 127.0.0.1 (any port).
        '[registry."127.0.0.1"]\n  http = true\n',
        encoding="utf-8",
    )
    name = f"buildkit-test-{uuid.uuid4().hex[:8]}"
    volume = f"buildkit-test-vol-{uuid.uuid4().hex[:8]}"
    start = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "--security-opt",
            "seccomp=unconfined",
            "--security-opt",
            "label=disable",
            "--network",
            "host",
            "-v",
            f"{cfg}:/home/user/.config/buildkit/buildkitd.toml:ro",
            "-v",
            f"{volume}:/home/user/.local/share/buildkit",
            "moby/buildkit:rootless",
        ],
        capture_output=True,
        text=True,
    )
    if start.returncode != 0:
        pytest.skip(f"cannot start buildkit container: {start.stderr.strip()}")
    container = start.stdout.strip()
    try:
        deadline = time.time() + 90
        ready = False
        while time.time() < deadline:
            state = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if state == "true":
                ready = True
                break
            time.sleep(0.5)
        if not ready:
            pytest.skip("buildkit did not become ready")
        yield f"tcp://127.0.0.1:{port}"
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


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

    def _make(*, control_settings=None, envd_settings=None):
        runtime_registry = RuntimeRegistry(workspace)
        control = create_control_app(
            settings=control_settings
            or ControlSettings(
                api_keys=("local-key",),
                create_queue_timeout_s=0,
            ),
            runtime_registry=runtime_registry,
            workspace_base=workspace,
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
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

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
    runtime_registry = RuntimeRegistry(PROJECT_ROOT / "tmp" / "sdk-workspace")
    control_port = _free_port()
    envd_port = _free_port()
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
            buildkit_addr=buildkitd,
        ),
        runtime_registry=runtime_registry,
        workspace_base=PROJECT_ROOT / "tmp" / "sdk-workspace",
    )
    envd_app = create_envd_app(
        settings=EnvdSettings(
            executor="local",
            envd_port=envd_port,
        ),
        runtime_registry=runtime_registry,
        workspace_base=PROJECT_ROOT / "tmp" / "sdk-workspace",
    )
    control = _ServerThread(control_app, control_port)
    envd = _ServerThread(envd_app, envd_port)
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
        PROJECT_ROOT / "tmp" / "multinode", 1, buildkit_addr=buildkitd
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
    root.mkdir(parents=True, exist_ok=True)
    control_dir = root / "control"
    shared_volumes = root / "shared-volumes"
    shared_workspace_dir = (
        root / "shared-workspace" if shared_workspace else control_dir
    )
    control_dir.mkdir(parents=True, exist_ok=True)
    shared_volumes.mkdir(parents=True, exist_ok=True)
    if shared_workspace:
        shared_workspace_dir.mkdir(parents=True, exist_ok=True)

    control_port = _free_port()
    gateway_port = _free_port()
    worker_ports = [_free_port() for _ in range(worker_count)]

    nodes = NodeRegistry(heartbeat_timeout=12)
    control_app = create_control_app(
        settings=ControlSettings(
            api_keys=("local-key",),
            control_plane_port=control_port,
            envd_port=worker_ports[0],
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
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
                    image_registry_username=image_registry_username,
                    image_registry_password=image_registry_password,
                    **envd_extra,
                ),
                runtime_registry=RuntimeRegistry(worker_base),
                workspace_base=worker_base,
                control_plane_url=f"http://127.0.0.1:{control_port}",
                node_address=f"http://127.0.0.1:{worker_port}",
            )
        )
    gateway_app = create_gateway(
        control_plane_url=f"http://127.0.0.1:{control_port}",
        internal_api_key="internal-key",
    )

    control = _ServerThread(control_app, control_port)
    workers = [_ServerThread(app, port) for app, port in zip(worker_apps, worker_ports)]
    gateway = _ServerThread(gateway_app, gateway_port)
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
        PROJECT_ROOT / "tmp" / "multinode-two",
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
        PROJECT_ROOT / "tmp" / "multinode-shared",
        2,
        shared_workspace=True,
        buildkit_addr=buildkitd,
    )
    yield harness
    harness["_stop"]()


@pytest.fixture(scope="session")
def image_registry_url():
    """A disposable Docker registry (``registry:2``) on a random port."""
    if shutil.which("docker") is None:
        pytest.skip("docker is required for the image registry tests")
    port = _free_port()
    start = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            f"127.0.0.1:{port}:5000",
            "registry:2",
        ],
        capture_output=True,
        text=True,
    )
    if start.returncode != 0:
        pytest.skip(f"cannot start registry container: {start.stderr.strip()}")
    container_id = start.stdout.strip()
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
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        pytest.skip("registry container did not become ready in time")
    yield f"127.0.0.1:{port}"
    subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)


@pytest.fixture(scope="session")
def authenticated_registry():
    """A Docker registry requiring basic auth (bcrypt htpasswd)."""
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
        pytest.skip(f"cannot generate htpasswd: {ht.stderr.strip()}")
    # The htpasswd file is bind-mounted by a docker CLI running inside the
    # test container. The daemon resolves the -v source path on the HOST, so
    # the file must physically exist at host_root — which, when running
    # inside the container, means writing through the /workspace mount (the
    # container view of the same host tree), not the host-absolute path
    # (that would land in the container's own filesystem and the daemon
    # would create a directory at the missing host path).
    host_root = Path(os.environ.get("E2B_HOST_PROJECT") or str(PROJECT_ROOT))
    write_root = Path("/workspace") if os.environ.get("E2B_HOST_PROJECT") else PROJECT_ROOT
    htpasswd_dir = write_root / "tmp" / "registry-auth"
    htpasswd_dir.mkdir(parents=True, exist_ok=True)
    htpasswd_path = htpasswd_dir / "htpasswd"
    # Self-heal: if a previous run left a directory here (docker creates
    # missing bind sources as dirs), the registry would mount a directory as
    # the htpasswd file and return 400 on login.
    if htpasswd_path.is_dir():
        shutil.rmtree(htpasswd_path)
    htpasswd_path.write_text(ht.stdout, encoding="utf-8")
    mount_src = host_root / "tmp" / "registry-auth" / "htpasswd"

    port = _free_port()
    start = subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "-p",
            f"127.0.0.1:{port}:5000",
            "-e",
            "REGISTRY_AUTH=htpasswd",
            "-e",
            "REGISTRY_AUTH_HTPASSWD_PATH=/auth/htpasswd",
            "-e",
            "REGISTRY_AUTH_HTPASSWD_REALM=Registry",
            "-v",
            f"{mount_src}:/auth/htpasswd",
            "registry:2",
        ],
        capture_output=True,
        text=True,
    )
    if start.returncode != 0:
        pytest.skip(f"cannot start authenticated registry: {start.stderr.strip()}")
    container_id = start.stdout.strip()
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
        subprocess.run(["docker", "rm", "-f", container_id], capture_output=True)
        pytest.skip("authenticated registry did not become ready in time")
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
    workspace = PROJECT_ROOT / "tmp" / name
    runtime_registry = RuntimeRegistry(workspace)
    control_port = _free_port()
    envd_port = _free_port()
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
    control = _ServerThread(control_app, control_port)
    envd = _ServerThread(envd_app, envd_port)
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
        PROJECT_ROOT / "tmp" / "multinode-registry",
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
        PROJECT_ROOT / "tmp" / "multinode-registry-auth",
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
