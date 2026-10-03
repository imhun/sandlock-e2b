"""N65: a volume pin is a fact about the bytes, not a deployment's memory.

Three claims are pinned here.

* The create path derives no pin from ``snapshot.node_id``: a snapshot is a tar
  on the shared volume, so pinning its create to the node that happened to
  capture it bought no locality -- and the old ``elif`` let a snapshot skip the
  volume check entirely when a snapshot and a node-local volume were both in
  play.
* "Is this volume shared?" is answered from where the volume's path actually
  lives -- under ``E2B_SHARED_VOLUME_ROOT`` (when it is named) or under the
  platform root the store is derived from when it is not -- rather than from
  whether the deployment remembered to set that env. The live CP shape N65 was
  found in left it unset, so ``POST /volumes`` recorded the default
  ``node_id="local"`` and every create with a volume computed a pin to a node
  no worker has.
* A pin that names a node no candidate can be is **named** -- a WARNING with the
  pinned node, the gate that kept it out, and the fallback -- instead of being
  dropped silently. It stays a fallback (not a ``503``): a nominal pin must not
  refuse a legal create.

The manifest pin is the deploy half, and it pins the *invariant*, not a shared
spelling: the control plane's volume store must land on ``<platform_root>/
"_volumes"`` -- the directory this pod holds writable -- so it must not name
``E2B_SHARED_VOLUME_ROOT`` at all. On the control plane that name is the store
root *itself* (``app.py``: ``volume_root = settings.shared_volume_root or
platform_root / "_volumes"``), while on the worker it is the *export root* the
agent scopes hostPaths to. One name, two meanings: naming it on the control
plane points the store at the read-only mount and every ``POST /volumes`` is
``EROFS`` (0.1.0-943).
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import yaml

from control_plane.api import snapshots as snapshots_api
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from control_plane.registry.nodes import NodeRegistry
from envd_service.runtime.registry import RuntimeRegistry

REPO = Path(__file__).resolve().parent.parent.parent
K8S_CONTROL_PLANE = REPO / "deploy" / "k8s" / "control-plane.yaml"
K8S_WORKER = REPO / "deploy" / "k8s" / "worker.yaml"

API_KEY = "local-key"
DIMS = {"memory_mb": 512, "cpu_percent": 100, "disk_mb": 1024, "processes": 64}


# --------------------------------------------------------------- create path


def _control_app(tmp_path: Path):
    """A control plane with **no** shared-*root* env -- the shape N65 found.

    ``workspace_base`` is the whole of what it names, so ``platform_root`` (and
    therefore the volume store) is ``workspace_base``; a volume the API creates
    lands under ``<workspace_base>/_volumes``, which is exactly the path the
    judge has to read as "shared".
    """
    workspace = tmp_path / "export"
    workspace.mkdir(exist_ok=True)
    settings = ControlSettings(
        api_keys=(API_KEY,),
        workspace_base=workspace,
        # Keep the capacity path fast: this lane stubs placement out.
        eviction_enabled=False,
        create_queue_timeout_s=0,
    )
    return create_control_app(
        settings=settings,
        runtime_registry=RuntimeRegistry(workspace),
        workspace_base=workspace,
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("10.0.0.1", 44444)),
        base_url="http://control",
    )


def _spy_placement(app) -> dict:
    """Record the admission kwargs and let the real placement run."""
    seen: dict = {}
    original = app.state.select_node

    def spy(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    app.state.select_node = spy
    return seen


def _spy_fork_placement(app) -> dict:
    """Same, for the fork path.

    ``_create_sandbox_from_snapshot`` reaches for
    ``app.state.nodes.select_and_reserve`` itself instead of the
    ``app.state.select_node`` alias the create admission goes through, so the
    spy has to sit on the registry method the fork actually calls.
    """
    seen: dict = {}
    original = app.state.nodes.select_and_reserve

    def spy(**kwargs):
        seen.update(kwargs)
        return original(**kwargs)

    app.state.nodes.select_and_reserve = spy
    return seen


async def test_a_volume_on_the_platform_root_is_not_a_pin(tmp_path) -> None:
    """The CP-missing-env shape: the store is under the platform root, so the
    volume is shared and no pin is passed (today this computes ``"local"``)."""
    app = _control_app(tmp_path)
    seen = _spy_placement(app)
    async with _client(app) as client:
        created = await client.post(
            "/volumes",
            headers={"X-API-Key": API_KEY},
            json={"name": "shared-vol", "perSandboxQuotaMb": 0},
        )
        assert created.status_code == 201
        volume_id = created.json()["volumeID"]
        volume = app.state.volumes.get(volume_id)

        # The deployment really is the N65 shape, and the volume really does
        # live under the root the store was derived from.
        assert app.state.settings.shared_volume_root is None
        assert app.state.platform_root == (tmp_path / "export")
        assert volume.path.is_relative_to(app.state.platform_root)

        sandbox = await client.post(
            "/sandboxes",
            headers={"X-API-Key": API_KEY},
            json={
                "templateID": "base",
                "volumeMounts": [{"name": volume_id, "path": "mnt/data"}],
            },
        )
    assert sandbox.status_code == 201, sandbox.text
    assert seen["volume_node_id"] is None


async def test_a_snapshot_create_carries_no_pin_from_the_snapshot(tmp_path) -> None:
    """A snapshot is a tar on the shared volume; its ``node_id`` is not a pin."""
    app = _control_app(tmp_path)
    seen = _spy_placement(app)
    source = tmp_path / "captured"
    (source / "workspace").mkdir(parents=True)
    (source / "workspace" / "data.txt").write_text("payload", encoding="utf-8")
    snapshot = app.state.snapshots.create_from_sandbox(
        workspace_dir=source,
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        node_id="worker-that-captured-it",
        name="snap",
    )
    assert snapshot.node_id == "worker-that-captured-it"

    async with _client(app) as client:
        sandbox = await client.post(
            "/sandboxes",
            headers={"X-API-Key": API_KEY},
            json={"templateID": snapshot.snapshot_id},
        )
    assert sandbox.status_code == 201, sandbox.text
    # The snapshot still names the node that captured it; placement simply no
    # longer hears about it.
    assert seen["volume_node_id"] is None


async def test_a_fork_carries_no_pin_from_the_snapshot(tmp_path, monkeypatch) -> None:
    """``POST /sandboxes/{id}/fork`` builds from a snapshot through its own
    path, so the same no-pin rule has to hold there.

    Two snapshot-shaped creates with two answers would be the semantic split
    the batch ruling closed: ``templateID=<snapID>`` unpinned, ``fork`` pinned
    -- and a pinned fork answers ``503`` while another node sits empty (the
    N60 shape on a second endpoint).
    """
    app = _control_app(tmp_path)
    seen = _spy_fork_placement(app)
    # The fleet knows the node that captured the snapshot, so the pin really
    # binds a node today instead of being dropped as a ghost. It gets the
    # in-process address because this lane is about the kwargs placement
    # receives, not about a worker round trip: a bogus remote address would
    # only add an HTTP timeout to the red run.
    _register(
        app.state.nodes,
        "worker-that-captured-it",
        address="local://",
        labels={"node-type": "local"},
    )
    source = tmp_path / "captured-for-fork"
    (source / "workspace").mkdir(parents=True)
    (source / "workspace" / "data.txt").write_text("payload", encoding="utf-8")
    snapshot = app.state.snapshots.create_from_sandbox(
        workspace_dir=source,
        template_id="base",
        env_vars={},
        metadata={},
        volume_mounts=[],
        base_image=None,
        allow_internet_access=False,
        node_id="worker-that-captured-it",
        name="snap",
    )
    assert snapshot.node_id == "worker-that-captured-it"
    # A fork captures first, and that capture is a worker round trip; this test
    # is about the placement the fork then makes, so the capture is stubbed to
    # the snapshot already on disk.
    monkeypatch.setattr(
        snapshots_api,
        "_capture_snapshot",
        lambda request, sandbox_id, name=None: snapshot,
    )

    async with _client(app) as client:
        response = await client.post(
            "/sandboxes/sbx_captured/fork",
            headers={"X-API-Key": API_KEY},
            json={"timeout": 300},
        )
    assert response.status_code == 201, response.text
    results = response.json()
    assert len(results) == 1
    assert "error" not in results[0]
    assert results[0]["sandbox"]["templateID"] == "base"
    # The snapshot still names the node that captured it; placement simply no
    # longer hears about it.
    assert seen["volume_node_id"] is None


# ------------------------------------------------------------- pin goes missing


def _register(nodes: NodeRegistry, node_id: str, **overrides) -> None:
    kwargs = dict(
        node_id=node_id,
        address=f"http://10.0.0.{len(nodes.list()) + 1}:49983",
        total_memory_mb=4096,
        total_cpu_percent=400,
        total_disk_mb=8192,
        total_processes=256,
    )
    kwargs.update(overrides)
    nodes.register(**kwargs)


def _warnings(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.WARNING
    ]


def test_a_pin_to_a_node_that_does_not_exist_is_named_and_falls_back(caplog) -> None:
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    _register(nodes, "node_a")
    _register(nodes, "node_b")

    with caplog.at_level(logging.WARNING):
        node = nodes.select_and_reserve(
            base_image=None, volume_node_id="ghost", **DIMS
        )

    # The placement still succeeds (a nominal pin must not refuse a create)...
    assert node is not None
    # ...and which node it landed on is visible.
    assert node.node_id in {"node_a", "node_b"}
    assert _warnings(caplog) == [
        "volume pin to node ghost could not be honoured (there is no such node "
        "in the fleet); the pin is ignored and placement falls back to the "
        "ranked candidates"
    ]


def test_a_pin_to_a_full_node_names_the_dimension_and_falls_back(caplog) -> None:
    nodes = NodeRegistry(heartbeat_timeout=600.0)
    _register(nodes, "node_small", total_memory_mb=256)
    _register(nodes, "node_roomy")

    with caplog.at_level(logging.WARNING):
        node = nodes.select_and_reserve(
            base_image=None, volume_node_id="node_small", **DIMS
        )

    assert node is not None
    assert node.node_id == "node_roomy"
    assert _warnings(caplog) == [
        "volume pin to node node_small could not be honoured (it does not fit "
        "the sandbox (memory)); the pin is ignored and placement falls back to "
        "the ranked candidates"
    ]


# ------------------------------------------------------------- manifest pin


def _container(path: Path, kind: str, name: str, container: str) -> dict:
    """The named container of one workload document, parsed rather than grepped."""
    docs = [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]
    matches = [
        doc
        for doc in docs
        if doc.get("kind") == kind
        and (doc.get("metadata") or {}).get("name") == name
    ]
    assert len(matches) == 1, f"{path}: expected one {kind}/{name}: {len(matches)}"
    containers = [
        c
        for c in matches[0]["spec"]["template"]["spec"]["containers"]
        if c["name"] == container
    ]
    assert len(containers) == 1, f"{path}: expected one {container}: {containers}"
    return containers[0]


def _env_entries(container: dict, name: str) -> list[dict]:
    return [e for e in container.get("env") or [] if e.get("name") == name]


def _env_value(container: dict, name: str) -> str | None:
    entries = _env_entries(container, name)
    assert len(entries) <= 1, f"duplicate env {name}: {entries}"
    return entries[0].get("value") if entries else None


def _reroot(root: str, tmp_path: Path) -> Path:
    """The manifest's absolute root, re-rooted under ``tmp_path``.

    The value -- and therefore the *relation* between two of them -- is kept;
    only the leading ``/`` moves, so the derivation can run on a host where
    ``/var/lib/e2b-sandboxes`` does not exist and is not writable.
    """
    return tmp_path / Path(root).relative_to("/")


def test_the_control_plane_volume_store_lands_on_its_writable_volumes_subpath(
    tmp_path,
) -> None:
    """(a) Derived, not textual: the env the manifest names must derive a store
    root equal to ``<platform_root>/"_volumes"`` -- the directory this pod holds
    writable.

    0.1.0-943 added ``E2B_SHARED_VOLUME_ROOT=/var/lib/e2b-sandboxes`` to this
    pod. On the control plane that name is the volume *store root itself*
    (``app.py``: ``volume_root = settings.shared_volume_root or platform_root /
    "_volumes"``), and ``/var/lib/e2b-sandboxes`` is exactly the mount this pod
    holds **read-only** (``_volumes`` is the writable subPath) -- so the first
    ``POST /volumes`` was ``EROFS``. The manifest is this test's *input*: its
    env is fed to ``create_app`` and the derived path is judged. A manifest that
    names the export root as the volume root makes the store land on the
    read-only mount and this assertion fails.
    """
    plane = _container(
        K8S_CONTROL_PLANE, "Deployment", "control-plane", "control-plane"
    )
    export = _env_value(plane, "E2B_SHARED_WORKSPACE_ROOT")
    assert export, "the control plane must name its shared export root"
    volume_root = _env_value(plane, "E2B_SHARED_VOLUME_ROOT")
    settings = ControlSettings(
        api_keys=(API_KEY,),
        workspace_base=_reroot(_env_value(plane, "E2B_WORKSPACE_BASE"), tmp_path),
        shared_workspace_root=str(_reroot(export, tmp_path)),
        shared_volume_root=str(_reroot(volume_root, tmp_path)) if volume_root else None,
        trees_shared=False,
        eviction_enabled=False,
        create_queue_timeout_s=0,
    )
    app = create_control_app(
        settings=settings, workspace_base=settings.workspace_base
    )
    # The derived store root is the manifest's own export root plus `_volumes`,
    # and no named store root hijacked it (`create_app` only reaches for
    # `shared_volume_root` when the deployment set one).
    assert app.state.platform_root == Path(settings.shared_workspace_root).resolve()
    assert app.state.volumes._base == app.state.platform_root / "_volumes"
    assert app.state.settings.shared_volume_root is None


def test_the_control_plane_mounts_its_volumes_store_read_write() -> None:
    """(b) The directory (a) derives must be writable in this pod's own mounts.

    The read-only parent plus writable subPaths is the OBS-9 shape. Without the
    ``_volumes`` subPath the derivation in (a) still "passes" while the store
    lands on the read-only export root again -- ``EROFS`` on the first create.
    """
    plane = _container(
        K8S_CONTROL_PLANE, "Deployment", "control-plane", "control-plane"
    )
    export = _env_value(plane, "E2B_SHARED_WORKSPACE_ROOT")
    assert export, "the control plane must name its shared export root"
    store = f"{export}/_volumes"
    mounts = [
        m for m in plane.get("volumeMounts") or [] if m.get("mountPath") == store
    ]
    assert len(mounts) == 1, f"expected one mount at {store}: {mounts}"
    assert mounts[0]["subPath"] == "_volumes", mounts[0]
    assert mounts[0].get("readOnly") is not True, mounts[0]


def test_the_worker_names_the_shared_volume_export_root() -> None:
    """(b) The worker's copy keeps its meaning: the export root the agent scopes.

    ``E2B_SHARED_VOLUME_ROOT`` on the worker is the *export root* the control
    plane's volume paths must also live under (``build_volume_mounts``, and the
    agent refusing a hostPath outside its roots). So it stays named here and it
    must equal the control plane's own export root -- the two manifests agree on
    the *directory*, which is the fact; "both pods set the same variable" was
    the 0.1.0-943 mistake.
    """
    worker = _container(K8S_WORKER, "StatefulSet", "e2b-worker", "worker")
    plane = _container(
        K8S_CONTROL_PLANE, "Deployment", "control-plane", "control-plane"
    )
    worker_root = _env_value(worker, "E2B_SHARED_VOLUME_ROOT")
    assert worker_root, "the worker must name the shared volume export root"
    assert worker_root == _env_value(plane, "E2B_SHARED_WORKSPACE_ROOT")
