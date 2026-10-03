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

The manifest pin is the deploy half: the control plane and the worker must
spell the shared volume root the same way, or the store's root and the judge's
input disagree.
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


def _shared_volume_root_entries(
    path: Path, kind: str, name: str, container: str
) -> list[dict]:
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
    env = containers[0].get("env") or []
    return [e for e in env if e.get("name") == "E2B_SHARED_VOLUME_ROOT"]


def test_the_control_plane_and_the_worker_name_the_same_shared_volume_root() -> None:
    """One value, both manifests: the store's root and the judge's input.

    The worker derives every volume host path from this and the agent refuses a
    hostPath outside it; the control plane derives the volume store from it and
    the create/migration judge reads it. A control plane whose copy is missing
    reads its volumes as node-local (N65), so the two lines must agree.
    """
    worker = _shared_volume_root_entries(
        K8S_WORKER, "StatefulSet", "e2b-worker", "worker"
    )
    plane = _shared_volume_root_entries(
        K8S_CONTROL_PLANE, "Deployment", "control-plane", "control-plane"
    )
    assert len(worker) == 1, worker
    assert len(plane) == 1, plane
    value = worker[0]["value"]
    assert value, "the worker's shared volume root must be a named path"
    assert plane[0]["value"] == value
