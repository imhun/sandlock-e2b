"""C3 Task 4: the worker asks the control plane, and never acts locally.

Two halves, and the second is the one the task exists for:

* the **wire**: every op is ``{op, sandbox_id, ...params}`` -- no path, no uid
  (hard rules 1/3, §14.4) -- to ``/internal/nodes/{node}/file-op`` with the
  worker's internal key;
* the **call sites**: the privileged steps that used to run ``e2b-maint`` here
  reach the agent instead. Each of those tests installs a recording stub and
  asserts both that the op was asked for *and* that the local privileged call
  did not happen (D18.1: no fallback, no silent skip).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import threading
from pathlib import Path

import httpx
import pytest

from envd_service import agent_fileops
from envd_service.agent_fileops import AgentFileOps, AgentFileOpsError
from gateway_common.paths import own_identity_instance_name

CP_URL = "http://control-plane:3000"
NODE = "e2b-worker-0"
KEY = "internal-key"
SANDBOX = "sbx_fileops"
VOLUME = "data"


def _client(handler, **overrides) -> AgentFileOps:
    options = dict(
        control_plane_url=CP_URL,
        node_id=NODE,
        internal_key=KEY,
        timeout_s=2.0,
        transport=httpx.MockTransport(handler),
    )
    options.update(overrides)
    return AgentFileOps(**options)


class _RecordingStub:
    """Stands in for the client: records ``(op, sandbox_id, params)``."""

    #: The ops whose real signature takes one positional parameter, in the same
    #: spelling the wire uses.
    POSITIONAL = {
        "chown-volume-slice": ("volume",),
        "chown-volume-root": ("volume",),
        "remove-volume-slice": ("volume",),
        "chown-secret": ("name",),
        "scope-slot-document": ("name",),
    }

    def __init__(self, *, refuse: str | None = None, stdout: str = "") -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._refuse = refuse
        self._stdout = stdout

    def _record(self, op: str, sandbox_id: str, **params):
        self.calls.append((op, sandbox_id, params))
        if self._refuse is not None:
            raise AgentFileOpsError(self._refuse)
        return {"op": op, "path": "/derived/by/the/control/plane", "stdout": self._stdout}

    def __getattr__(self, name: str):
        method = name.replace("_", "-")

        def _call(sandbox_id: str, *args, **params):
            names = self.POSITIONAL.get(method, ())
            if len(args) > len(names):
                raise AssertionError(f"unexpected positional arguments for {method}")
            for name, value in zip(names, args):
                params.setdefault(name, value)
            if method == "workspace-bytes":
                self._record("walk-workspace", sandbox_id)
                return sum(int(line.split()[4]) for line in self._stdout.splitlines())
            if method == "checkpoint-bytes":
                self._record("walk-checkpoint", sandbox_id)
                return sum(int(line.split()[4]) for line in self._stdout.splitlines())
            return self._record(method, sandbox_id, **params)

        return _call


@pytest.fixture()
def install_stub(monkeypatch):
    """Install a recording stub as the worker's active client."""

    def _install(stub) -> _RecordingStub:
        monkeypatch.setattr(agent_fileops, "_ACTIVE", [stub])
        return stub

    return _install


@pytest.fixture(autouse=True)
def _isolate_singletons(monkeypatch):
    """No test in this file may leak the module-level singletons.

    ``configure`` installs one, and the agent client holds its in a
    module-global list (that is the shape the worker uses at startup). A leaked
    one makes *later* tests run in a shape they never asked for -- which is
    exactly how this file's first version broke ``test_route_b_slot_identity``
    when the two were run in one session.
    """
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [None])


# ------------------------------------------------------------------- the wire


@pytest.mark.parametrize(
    "method, args, expected_op, expected_params",
    [
        ("chown_workspace", (), "chown-workspace", {"recursive": True}),
        ("remove_workspace", (), "remove-workspace", {}),
        ("walk_workspace", (), "walk-workspace", {}),
        ("remove_runtime", (), "remove-runtime", {}),
        (
            "chown_checkpoint",
            (),
            "chown-checkpoint",
            {"recursive": True},
        ),
        ("remove_checkpoint", (), "remove-checkpoint", {}),
        ("walk_checkpoint", (), "walk-checkpoint", {}),
        (
            "chown_volume_slice",
            (VOLUME,),
            "chown-volume-slice",
            {"volume": VOLUME, "recursive": True},
        ),
        ("chown_volume_root", (VOLUME,), "chown-volume-root", {"volume": VOLUME}),
        ("remove_volume_slice", (VOLUME,), "remove-volume-slice", {"volume": VOLUME}),
        ("chown_secret", ("github",), "chown-secret", {"name": "github"}),
        (
            "scope_slot_document",
            ("policy.json",),
            "scope-slot-document",
            {"name": "policy.json"},
        ),
    ],
)
def test_every_op_travels_as_sandbox_id_and_op_only(
    method, args, expected_op, expected_params
) -> None:
    """The wire contract: the request names the sandbox and the action.

    No path and no uid -- the control plane derives both from its own records
    (hard rule 3 / §14.4). The assertion is the *body*, not "some request went
    out": a field added here is a field the platform did not authorize.
    """
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"op": expected_op, "path": "/x", "stdout": ""})

    getattr(_client(handler), method)(SANDBOX, *args)
    assert len(seen) == 1
    request = seen[0]
    assert str(request.url) == f"{CP_URL}/internal/nodes/{NODE}/file-op"
    assert request.headers["X-Internal-Key"] == KEY
    assert json.loads(request.content) == {
        "op": expected_op,
        "sandbox_id": SANDBOX,
        **expected_params,
    }


def test_an_op_outside_the_vocabulary_is_refused_before_dialling() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    with pytest.raises(AgentFileOpsError) as excinfo:
        _client(handler).request("chown-everything", SANDBOX)
    assert str(excinfo.value) == (
        "'chown-everything' is not a file operation this worker may ask for"
    )
    assert seen == []


def test_a_refused_file_op_is_named_and_fail_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "code": 502,
                "message": (
                    "e2b-maint rm refused (exit 77): e2b-maint: refused: "
                    "/var/lib/e2b-sandboxes/workspaces/sbx_fileops is not "
                    "under any privileged helper root"
                ),
            },
        )

    with pytest.raises(AgentFileOpsError) as excinfo:
        _client(handler).remove_workspace(SANDBOX)
    assert str(excinfo.value) == (
        f"the control plane refused remove-workspace for sandbox {SANDBOX} "
        "(HTTP 502): e2b-maint rm refused (exit 77): e2b-maint: refused: "
        "/var/lib/e2b-sandboxes/workspaces/sbx_fileops is not under any "
        "privileged helper root"
    )


def test_an_unreachable_control_plane_is_named() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(AgentFileOpsError) as excinfo:
        _client(handler).chown_workspace(SANDBOX)
    assert str(excinfo.value) == (
        f"the control plane is unreachable for chown-workspace on sandbox "
        f"{SANDBOX}: connection refused"
    )


def test_a_non_json_answer_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="ok")

    with pytest.raises(AgentFileOpsError) as excinfo:
        _client(handler).remove_workspace(SANDBOX)
    assert str(excinfo.value) == (
        f"the control plane answered remove-workspace for sandbox {SANDBOX} "
        "with a non-JSON body"
    )


def test_a_walk_answer_without_entry_text_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"op": "walk-workspace", "path": "/x"})

    with pytest.raises(AgentFileOpsError) as excinfo:
        _client(handler).walk_workspace(SANDBOX)
    assert str(excinfo.value) == (
        f"the control plane's walk-workspace answer for sandbox {SANDBOX} "
        "carried no entry text"
    )


def test_walk_bytes_use_the_platforms_own_parser() -> None:
    stdout = (
        f"d 10007 10007 770 512 /var/lib/e2b-sandboxes/workspaces/{SANDBOX}\n"
        f"f 10007 10007 644 4096 /var/lib/e2b-sandboxes/workspaces/{SANDBOX}/a.txt\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"op": "walk-workspace", "stdout": stdout})

    assert _client(handler).workspace_bytes(SANDBOX) == 4608


# ------------------------------------------------------------- the singleton


def test_the_agent_shape_needs_a_control_plane_and_a_node_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Settings:
        priv_helper_transport = "agent"
        internal_api_key = KEY

    monkeypatch.delenv("E2B_CONTROL_PLANE_URL", raising=False)
    monkeypatch.delenv("E2B_NODE_ID", raising=False)
    with pytest.raises(AgentFileOpsError) as excinfo:
        agent_fileops.configure(_Settings())
    assert str(excinfo.value) == (
        "E2B_PRIV_HELPER_TRANSPORT=agent needs E2B_CONTROL_PLANE_URL and "
        "E2B_NODE_ID: refusing to start without a control plane to ask"
    )

    monkeypatch.setenv("E2B_CONTROL_PLANE_URL", CP_URL)
    monkeypatch.setenv("E2B_NODE_ID", NODE)
    client = agent_fileops.configure(_Settings())
    assert client is not None
    assert agent_fileops.active() is client
    assert agent_fileops.enabled(_Settings()) is True


def test_any_other_transport_leaves_the_singleton_unset() -> None:
    class _Settings:
        priv_helper_transport = "auto"

    assert agent_fileops.configure(_Settings()) is None
    assert agent_fileops.active() is None


def test_the_shape_is_read_from_the_transport_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production has no settings field for it: the env var is the switch."""

    class _Settings:
        internal_api_key = KEY

    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "agent")
    monkeypatch.setenv("E2B_CONTROL_PLANE_URL", CP_URL)
    monkeypatch.setenv("E2B_NODE_ID", NODE)
    client = agent_fileops.configure(_Settings())
    assert client is not None
    assert agent_fileops.enabled(_Settings()) is True
    # ...and the same variable is what ``priv_helpers`` reads, so the two
    # halves of the shape cannot disagree: the client it wires is the one the
    # worker's file steps go through.
    from envd_service import priv_helpers

    agent_fileops.configure(_Settings())
    assert priv_helpers.configure_priv_helpers(_Settings()) is None
    assert agent_fileops.active() is not None


# ------------------------------------------------------------- the call sites


def test_the_ownership_handover_goes_to_the_agent(
    tmp_path: Path, install_stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provisioning: the tree is handed over by the agent, not in-process."""
    from envd_service import uid_pool

    tree = tmp_path / "workspaces" / SANDBOX
    tree.mkdir(parents=True)
    (tree / "workspace").mkdir()
    stub = install_stub(_RecordingStub())

    def _forbidden(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the worker performed a privileged chown itself")

    monkeypatch.setattr(uid_pool, "_chown_tree", _forbidden)
    monkeypatch.setattr(uid_pool.os, "lchown", _forbidden)
    uid_pool.apply_sandbox_ownership(tree, 10007, sandbox_id=SANDBOX)
    assert stub.calls == [("chown-workspace", SANDBOX, {"recursive": True})]


def test_the_ownership_handover_needs_a_sandbox_id_in_the_agent_shape(
    tmp_path: Path, install_stub
) -> None:
    from envd_service import priv_helpers, uid_pool

    tree = tmp_path / "workspaces" / SANDBOX
    tree.mkdir(parents=True)
    stub = install_stub(_RecordingStub())
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        uid_pool.apply_sandbox_ownership(tree, 10007)
    assert str(excinfo.value) == (
        "the C3 agent shape needs the sandbox id to hand a tree over: "
        f"{tree} was not named by one"
    )
    assert stub.calls == []


def test_the_ownership_handover_sets_the_tree_modes_before_it_leaves(
    tmp_path: Path, install_stub
) -> None:
    """The mode pass is part of the hand-over, not an alternative to it (I-1).

    ``e2b-maint chown`` changes ownership and nothing else, and the worker is a
    *group member* of the tree afterwards, not its owner: a tree left at
    ``mkdir``'s 0755 would give the worker ``r-x`` on the workspace it must
    write for the files API, snapshots and lifecycle. So the assertion is the
    mode **on disk**, and the op is only half of it.
    """
    from envd_service import uid_pool

    tree = tmp_path / "workspaces" / SANDBOX
    (tree / "workspace").mkdir(parents=True)
    (tree / "workspace" / "note.txt").write_text("x", encoding="utf-8")
    for directory in (tree, tree / "workspace"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o755  # the create's default
    stub = install_stub(_RecordingStub())

    uid_pool.apply_sandbox_ownership(tree, UID_X, sandbox_id=SANDBOX)

    assert stub.calls == [("chown-workspace", SANDBOX, {"recursive": True})]
    assert stat.S_IMODE(tree.stat().st_mode) == 0o770
    assert stat.S_IMODE((tree / "workspace").stat().st_mode) == 0o770
    # Files keep their own modes (the sandbox may need them readable; only
    # directories need the group-writable bit for the worker).
    assert stat.S_IMODE((tree / "workspace" / "note.txt").stat().st_mode) == 0o644


def test_checkpoint_chown_walk_and_remove_go_to_the_agent(
    tmp_path: Path, install_stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from envd_service.runtime import checkpoint_store

    image = tmp_path / "state" / "_runtime" / ".checkpoints" / SANDBOX
    image.mkdir(parents=True)
    stdout = f"d 10007 65534 700 512 {image}\n"
    stub = install_stub(_RecordingStub(stdout=stdout))

    def _forbidden(*args, **kwargs):  # pragma: no cover - must not be reached
        raise AssertionError("the worker performed a privileged step itself")

    monkeypatch.setattr(checkpoint_store.os, "chown", _forbidden)
    monkeypatch.setattr(checkpoint_store, "_broker_chown_forbidden", None, raising=False)
    checkpoint_store._hand_to_sandbox(image, 10007, sandbox_id=SANDBOX)
    assert stub.calls == [("chown-checkpoint", SANDBOX, {"recursive": False})]
    assert checkpoint_store.image_bytes(image, sandbox_id=SANDBOX) == 512
    checkpoint_store._remove_image(image, sandbox_id=SANDBOX)
    assert stub.calls[1:] == [
        ("walk-checkpoint", SANDBOX, {}),
        ("remove-checkpoint", SANDBOX, {}),
    ]


def test_a_volume_slice_is_removed_by_the_agent(
    tmp_path: Path, install_stub
) -> None:
    from envd_service import volumes

    slice_dir = tmp_path / VOLUME / SANDBOX
    slice_dir.mkdir(parents=True)
    stub = install_stub(_RecordingStub())
    volumes.cleanup_volume_projects(
        volume_projects=[
            {
                "volume_id": VOLUME,
                "sandbox_id": SANDBOX,
                "mount_path": "data",
                "sandbox_dir": str(slice_dir),
                "projid": 0,
            }
        ],
        fallback_mount_point=tmp_path,
        via_agent=False,
    )
    assert stub.calls == [("remove-volume-slice", SANDBOX, {"volume": VOLUME})]
    assert slice_dir.is_dir()


def test_a_volume_record_without_a_name_is_refused_not_guessed(
    tmp_path: Path, install_stub
) -> None:
    from envd_service import priv_helpers, volumes

    slice_dir = tmp_path / VOLUME / SANDBOX
    slice_dir.mkdir(parents=True)
    stub = install_stub(_RecordingStub())
    with pytest.raises(priv_helpers.PrivHelperError) as excinfo:
        volumes.cleanup_volume_projects(
            volume_projects=[
                {
                    "sandbox_id": SANDBOX,
                    "mount_path": "data",
                    "sandbox_dir": str(slice_dir),
                    "projid": 0,
                }
            ],
            fallback_mount_point=tmp_path,
            via_agent=False,
        )
    entry = {
        "sandbox_id": SANDBOX,
        "mount_path": "data",
        "sandbox_dir": str(slice_dir),
        "projid": 0,
    }
    assert str(excinfo.value) == (
        f"the C3 agent shape cannot remove the slice of sandbox {SANDBOX}: "
        f"its volume name is not in the record ({entry!r})"
    )
    assert stub.calls == []


def test_the_slot_documents_are_scoped_by_the_agent(
    tmp_path: Path, install_stub
) -> None:
    """The brief's landmine: the policy carries the egress-proxy credentials."""
    from envd_service.own_identity import W1SlotPool

    # The leaf the worker really creates is the shared rule's answer (D20); this
    # test is about which *op* carries the scoping, and the leaf it names is the
    # one the peer pin in ``test_c3_slot_document_naming`` compares against the
    # CP's derivation.
    document = tmp_path / "10007" / own_identity_instance_name(SANDBOX) / "policy.json"
    document.parent.mkdir(parents=True)
    document.write_text("{}", encoding="utf-8")
    stub = install_stub(_RecordingStub())
    W1SlotPool._scope_slot_document(document, 10007, sandbox_id=SANDBOX)
    assert stub.calls == [
        ("scope-slot-document", SANDBOX, {"name": "policy.json"})
    ]


def test_a_refused_scoping_does_not_fall_back_to_a_world_readable_document(
    tmp_path: Path, install_stub
) -> None:
    """D18.1 on the one path where the old fallback leaked credentials.

    ``_write_slot_documents`` used to catch a failed chown and write the
    document ``0444`` -- world-readable, and the policy holds the egress-proxy
    credentials (the brief calls this out by file and line). In the agent shape
    a refusal is the end of the line.
    """
    from envd_service.own_identity import W1SlotPool

    root = tmp_path / "slots"
    leaf = own_identity_instance_name(SANDBOX)
    document = root / "10007" / leaf / "policy.json"
    document.parent.mkdir(parents=True)
    document.write_text("{}", encoding="utf-8")
    install_stub(_RecordingStub(refuse="the control plane refused it"))

    pool = object.__new__(W1SlotPool)
    with pytest.raises(AgentFileOpsError) as excinfo:
        W1SlotPool._write_slot_documents(
            pool,
            root,
            document.parent.parent,
            document.parent,
            document,
            document.parent / "program.json",
            {"policy": "secret"},
            {"program": "x"},
            10007,
            leaf,
            SANDBOX,
        )
    assert str(excinfo.value) == "the control plane refused it"
    assert oct(document.stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------- the shape, end to end (review)

#: A pooled uid the CP would have allocated (``E2B_UID_POOL_START`` + 7).
UID_X = 10007


class _AgentStub:
    """A client that records the ops *and* performs what the agent would.

    The create/delete endpoints check the disk after the op (that is the whole
    point of ``_remove_half`` / the surviving-tree check), so a stub that only
    records would make every teardown look like a failure. This one removes the
    tree/runtime dir the real agent would remove, and ``fail=True`` is the
    shape a permission or IO refusal has from here: the op raises and the
    files stay.
    """

    def __init__(
        self, *, workspace_base: Path, state_base: Path, fail: bool = False
    ) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._workspace_base = workspace_base
        self._state_base = state_base
        self._fail = fail

    def _record(self, op: str, sandbox_id: str, **params):
        self.calls.append((op, sandbox_id, params))
        if self._fail:
            raise AgentFileOpsError(f"{op} refused by the agent (exit 77): boom")

    def chown_workspace(self, sandbox_id: str, *, recursive: bool = True) -> None:
        self._record("chown-workspace", sandbox_id, recursive=bool(recursive))

    def chown_volume_root(self, sandbox_id: str, volume: str) -> None:
        self._record("chown-volume-root", sandbox_id, volume=volume)

    def chown_volume_slice(
        self, sandbox_id: str, volume: str, *, recursive: bool = True
    ) -> None:
        self._record(
            "chown-volume-slice", sandbox_id, volume=volume, recursive=bool(recursive)
        )

    def remove_workspace(self, sandbox_id: str) -> None:
        path = self._workspace_base / sandbox_id
        self._record("remove-workspace", sandbox_id)
        if not path.exists():
            # ``e2b-maint rm`` refuses a path that is not there (``realpath`` →
            # NULL): the stub has to refuse the same way, or it would hide the
            # non-idempotency D19 is about.
            raise AgentFileOpsError(
                "remove-workspace refused by the agent (exit 77): e2b-maint: "
                f"refused: {path} does not exist"
            )
        shutil.rmtree(path, ignore_errors=True)

    def remove_runtime(self, sandbox_id: str) -> None:
        path = self._state_base / "_runtime" / sandbox_id
        self._record("remove-runtime", sandbox_id)
        if not path.exists():
            raise AgentFileOpsError(
                "remove-runtime refused by the agent (exit 77): e2b-maint: "
                f"refused: {path} does not exist"
            )
        shutil.rmtree(path, ignore_errors=True)

def _worker_app(workspace: Path, monkeypatch: pytest.MonkeyPatch):
    """A real envd app in the agent shape, with the stub installed after wiring.

    ``create_app`` runs the shape resolution (that is what makes the per-sandbox
    uid gate true), and the stub then replaces the singleton the same way a
    deployment's configured client would be used.
    """
    from envd_service.app import create_app as create_envd_app
    from envd_service.config import Settings as EnvdSettings
    from envd_service.runtime.registry import RuntimeRegistry

    monkeypatch.setenv("E2B_PRIV_HELPER_TRANSPORT", "agent")
    monkeypatch.setenv("E2B_CONTROL_PLANE_URL", CP_URL)
    monkeypatch.setenv("E2B_NODE_ID", NODE)
    monkeypatch.delenv("E2B_PER_SANDBOX_UID", raising=False)
    workspace_base = workspace / "workspaces"
    state_base = workspace / "state"
    settings = EnvdSettings(
        executor="local",
        workspace_base=workspace_base,
        state_base=state_base,
        shared_volume_root=None,
    )
    app = create_envd_app(
        settings=settings,
        runtime_registry=RuntimeRegistry(workspace_base, state_base=state_base),
    )
    stub = _AgentStub(workspace_base=workspace_base, state_base=state_base)
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [stub])
    return app, settings, stub


def _create_payload(sandbox_id: str) -> dict:
    return {
        "sandboxID": sandbox_id,
        "accessToken": "tok",
        "envVars": {},
        "baseImage": None,
        "memoryMB": 512,
        "cpuPercent": 100,
        "diskMB": 1024,
        "maxProcesses": 64,
        "allowInternetAccess": False,
        "maxCommandTimeout": 3600,
        "hostUID": UID_X,
    }


async def _post_create(app, sandbox_id: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.post(
            "/agent/sandboxes",
            headers={"X-Internal-Key": "internal-key"},
            json=_create_payload(sandbox_id),
        )


async def _delete(app, sandbox_id: str):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as client:
        return await client.delete(
            f"/agent/sandboxes/{sandbox_id}",
            headers={"X-Internal-Key": "internal-key"},
        )


def _agent_log(caplog) -> list[str]:
    """The lines ``envd_service.agent`` emitted, in order.

    Same shape as the repo's other exact-list helpers (``_warnings`` in
    ``test_xfs_project_quota_agent``): the *selection* is the module under test,
    and the assertion on what it emitted is exact. Without it the container
    lane's environment lines (seccomp missing, no ``CAP_SYS_PTRACE``) land in
    the middle of a list this test is not about.
    """
    return [
        record.message
        for record in caplog.records
        if record.name == "envd_service.agent"
    ]


async def test_the_create_endpoint_hands_the_tree_over_through_the_agent(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The task's core deliverable, driven through the real create endpoint.

    A gate that still asked ``active_helpers()`` left ``host_uid`` None in the
    agent shape, so the hand-over never ran and nothing said so: the tree kept
    the worker's identity and the whole face-B ``chown`` vocabulary was
    unreachable. This asserts the two things a silent gate cannot fake: the
    record carries the uid **and** the op went out.
    """
    app, _settings, stub = _worker_app(workspace, monkeypatch)
    resp = await _post_create(app, "sbx_handover")
    assert resp.status_code == 201
    record = app.state.runtime_registry.get("sbx_handover")
    assert record is not None
    assert record.host_uid == UID_X
    assert stub.calls == [("chown-workspace", "sbx_handover", {"recursive": True})]


async def test_the_volume_ownership_path_runs_under_the_agent_transport(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The second missed gate: ``_can_manage_sandbox_uid`` in ``volumes``.

    With the old predicate the volume root and the per-sandbox slice stayed in
    the worker's identity, silently: the shared-root model and the slice's
    ``0770`` are what make a volume mount work for a sandbox uid.
    """
    from envd_service import volumes
    from envd_service.xfs_quota import ProjectQuotaError  # noqa: F401  (contract)

    app, settings, stub = _worker_app(workspace, monkeypatch)
    volume_root = workspace / "_volumes" / "vol_a"
    volume_root.mkdir(parents=True)
    if os.geteuid() == 0:
        # The container lane runs as root, so the fixture root would be
        # root-owned and would additionally take the *root* hand-over
        # (``_ensure_shared_volume_root`` only moves a root-owned root) -- a
        # pre-existing policy pinned by its own case below, not part of this
        # one. Handing the fixture to the pooled uid keeps the case under test
        # (the slice) identical on both lanes.
        os.chown(volume_root, UID_X, UID_X)
    # Non-root: the gate must be answered by the *shape*, not by who runs the
    # test (the container lane runs this as root).
    monkeypatch.setattr(volumes.os, "geteuid", lambda: 65534)
    monkeypatch.setattr(
        volumes, "xfs_project_supported", lambda _mp, via_agent=False: (True, "")
    )
    monkeypatch.setattr(volumes, "provision_project", lambda **kwargs: 42)

    # The gate itself, pinned directly: it used to ask ``active_helpers()``,
    # which the agent shape leaves unset.
    assert volumes._can_manage_sandbox_uid() is True

    view, projid = volumes.provision_sandbox_volume_mount(
        sandbox_id="sbx_vol",
        volume_id="vol_abc123",
        mount_path="mnt/data",
        volume_path=volume_root,
        per_sandbox_quota_mb=512,
        fallback_mount_point=settings.workspace_base,
        via_agent=False,
        host_uid=UID_X,
    )
    assert view == volume_root / "sbx_vol"
    assert projid == 42
    # The slice hand-over is the op the mount needs. (The *root* hand-over is
    # guarded by "the root is still root-owned" -- pre-existing policy, pinned
    # by its own case below -- and this test's fixture root belongs to whoever
    # runs the suite.)
    assert stub.calls == [
        (
            "chown-volume-slice",
            "sbx_vol",
            {"volume": "vol_abc123", "recursive": True},
        ),
    ]


async def test_the_volume_root_hand_over_uses_the_volume_id(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The root half of the same model, wired to the same op vocabulary."""
    from envd_service import volumes

    _app, _settings, stub = _worker_app(workspace, monkeypatch)
    volume_root = workspace / "_volumes" / "vol_b"
    volume_root.mkdir(parents=True)

    volumes._chown_path(
        volume_root,
        UID_X,
        sandbox_id="sbx_vol",
        volume="vol_abc123",
        volume_root=True,
    )
    assert stub.calls == [("chown-volume-root", "sbx_vol", {"volume": "vol_abc123"})]
    assert volume_root.is_dir()


async def test_delete_removes_both_halves_through_the_agent(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, settings, stub = _worker_app(workspace, monkeypatch)
    await _post_create(app, "sbx_delete")
    tree = settings.workspace_base / "sbx_delete"
    runtime = Path(settings.state_base) / "_runtime" / "sbx_delete"
    runtime.mkdir(parents=True, exist_ok=True)
    stub.calls.clear()

    resp = await _delete(app, "sbx_delete")
    assert resp.status_code == 204
    assert stub.calls == [
        ("remove-workspace", "sbx_delete", {}),
        ("remove-runtime", "sbx_delete", {}),
    ]
    assert not tree.exists()
    assert not runtime.exists()


async def test_delete_is_idempotent_when_the_paths_are_already_absent(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D19: a retried delete is a success, and it says so by name.

    ``e2b-maint rm`` refuses a path that is not there, while a *teardown* must
    stay idempotent: the CP retries, and a sandbox that never materialised its
    runtime dir was never removable in the first place.
    """
    app, _settings, stub = _worker_app(workspace, monkeypatch)
    caplog.set_level("INFO", logger="envd_service.agent")

    first = await _delete(app, "sbx_twice")
    assert first.status_code == 204
    assert [call[0] for call in stub.calls] == []  # nothing existed to remove
    assert _agent_log(caplog) == [
        "agent delete sbx_twice: the tree of sbx_twice is already absent; "
        "nothing to remove",
        "agent delete sbx_twice: the platform state of sbx_twice is already "
        "absent; nothing to remove",
    ]

    caplog.clear()
    # ...and a second delete of the same (now really gone) sandbox is the same
    # answer, not a failure.
    created = await _post_create(app, "sbx_twice")
    assert created.status_code == 201
    assert (await _delete(app, "sbx_twice")).status_code == 204
    caplog.clear()
    second = await _delete(app, "sbx_twice")
    assert second.status_code == 204
    assert _agent_log(caplog) == [
        "agent delete sbx_twice: the tree of sbx_twice is already absent; "
        "nothing to remove",
        "agent delete sbx_twice: the platform state of sbx_twice is already "
        "absent; nothing to remove",
    ]
    assert [call[0] for call in stub.calls] == [
        "chown-workspace",
        "remove-workspace",
        "remove-runtime",
    ]


async def test_a_removal_that_leaves_the_tree_behind_still_fails_closed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """D19's other half: only *absent* is success.

    A refusal that leaves the files in place (a permission error, an IO error)
    must still fail closed and be named -- the worker may not read "the agent
    refused" as "the sandbox is gone", or the CP drops a record whose tree is
    still on the disk.
    """
    app, settings, stub = _worker_app(workspace, monkeypatch)
    await _post_create(app, "sbx_stuck")
    tree = settings.workspace_base / "sbx_stuck"
    assert tree.is_dir()
    stub._fail = True

    resp = await _delete(app, "sbx_stuck")
    assert resp.status_code == 500
    assert resp.text == (
        "the tree of sbx_stuck could not be removed "
        f"({tree}): AgentFileOpsError: remove-workspace refused by the agent "
        "(exit 77): boom"
    )
    assert tree.is_dir()


async def test_the_disabled_orphan_sweep_is_named_at_startup(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D18.1: a switch this shape overrules is said out loud, once."""
    # The root lane additionally discloses "no CAP_SYS_PTRACE" on this logger,
    # which is a different decision (E5.1's in-process RunAs) and is pinned by
    # its own tests; reporting the capability as present keeps this list about
    # the C3 line alone, on both lanes. (It is emitted from ``create_app``, so
    # the patch has to land before the app is built.)
    from envd_service import app as app_module

    monkeypatch.setattr(app_module, "has_effective_cap", lambda cap: True)
    app, _settings, _stub = _worker_app(workspace, monkeypatch)
    caplog.set_level("WARNING", logger="envd_service.app")
    async with app.router.lifespan_context(app):
        pass
    assert [
        record.message
        for record in caplog.records
        if record.name == "envd_service.app"
    ] == [
        "C3 agent shape: the worker's own uid-reconcile sweep is disabled -- "
        "reclaiming an orphan tree needs `chown --worker`, which this shape "
        "does not offer; orphan trees stay on disk until the control plane "
        "reclaims them (Task 6). E2B_UID_RECONCILE_ON_STARTUP has no effect in "
        "this shape",
    ]


def test_a_sandbox_the_control_plane_does_not_know_is_its_own_error() -> None:
    """A 404 is not "the control plane is broken" -- it says the sandbox is gone.

    The two used to be the same ``AgentFileOpsError``, so every caller could
    only retry. For the *record* that is the wrong answer: a runtime record this
    worker holds for a sandbox the control plane has forgotten can never be
    used again (every op is authorized against the CP's records), and retrying
    is what produced the measured storm -- see
    ``tests/unit/test_c3_fileop_degradation.py::
    test_a_record_the_control_plane_does_not_know_is_dropped``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404, json={"message": f"Sandbox {SANDBOX} not found"}
        )

    client = _client(handler)
    with pytest.raises(agent_fileops.AgentFileOpsUnknownSandbox) as excinfo:
        client.workspace_bytes(SANDBOX)

    assert str(excinfo.value) == (
        f"the control plane refused walk-workspace for sandbox {SANDBOX} "
        f"(HTTP 404): Sandbox {SANDBOX} not found"
    )
    # ...and it is still the failure every existing handler catches.
    assert isinstance(excinfo.value, AgentFileOpsError)


def _counting_clients(monkeypatch) -> list:
    """Record every ``httpx.Client`` this process builds, still really building it.

    The change under test is *how many connections* an op costs, so the count
    has to come from the transport's own class rather than from a stub that
    replaces it.
    """
    created: list = []
    real_client = httpx.Client

    class _Counting(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

    monkeypatch.setattr(httpx, "Client", _Counting)
    return created


def test_one_keep_alive_client_serves_every_operation(monkeypatch) -> None:
    """A create's ownership hand-over must not pay a fresh TCP connect.

    Measured on the fleet 2026-10-01: a ``walk-workspace`` that did no work
    cost 27 ms while the ``chown`` that actually walked the tree cost 49 ms --
    the difference was this connection setup, paid once per op because each
    call built its own ``httpx.Client``.
    """
    created = _counting_clients(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stdout": ""})

    client = _client(handler)
    client.walk_workspace(SANDBOX)
    client.request("chown-workspace", SANDBOX, recursive=True)

    assert len(created) == 1
    assert created[0] is client._client


def test_closing_the_client_returns_the_connection(monkeypatch) -> None:
    """``close()`` is the shutdown path: the next op dials again, on purpose."""
    created = _counting_clients(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stdout": ""})

    client = _client(handler)
    client.walk_workspace(SANDBOX)
    client.close()
    assert client._client is None
    client.walk_workspace(SANDBOX)
    assert len(created) == 2


def test_the_singleton_shutdown_closes_the_active_client(monkeypatch) -> None:
    """The worker's lifespan calls this; a client left open pins the socket."""
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [None])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stdout": ""})

    client = _client(handler)
    agent_fileops._ACTIVE[0] = client
    client.walk_workspace(SANDBOX)
    assert client._client is not None

    agent_fileops.shutdown()
    assert client._client is None
    agent_fileops.shutdown()  # idempotent


def test_concurrent_ops_build_exactly_one_client(monkeypatch) -> None:
    """One worker asks from several threads at once -- creates, teardowns and
    the disk-scan rounds all come through this client.

    Without the guard the first two ops to arrive each build one, and the
    loser's connection is never used by anybody -- a socket nobody closes.
    """
    created = _counting_clients(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"stdout": ""})

    client = _client(handler)
    start = threading.Barrier(8)

    def worker(worker_id: int) -> None:
        start.wait(timeout=5)
        client.request("chown-workspace", f"sbx_{worker_id:016x}", recursive=True)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(created) == 1
