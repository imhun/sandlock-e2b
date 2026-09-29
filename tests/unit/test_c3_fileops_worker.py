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
from pathlib import Path

import httpx
import pytest

from envd_service import agent_fileops
from envd_service.agent_fileops import AgentFileOps, AgentFileOpsError

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

    ``configure`` installs one, and both modules hold theirs in a module-global
    list (that is the shape the worker uses at startup). A leaked one makes
    *later* tests run in a shape they never asked for -- which is exactly how
    this file's first version broke ``test_route_b_slot_identity`` when the two
    were run in one session.
    """
    from envd_service import priv_helpers

    monkeypatch.setattr(agent_fileops, "_ACTIVE", [None])
    monkeypatch.setattr(priv_helpers, "_ACTIVE", [None])


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
    # halves of the shape cannot disagree.
    from envd_service import priv_helpers

    monkeypatch.setattr(priv_helpers, "_ACTIVE", [object()])
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
    assert str(excinfo.value).startswith(
        f"the C3 agent shape cannot remove the slice of sandbox {SANDBOX}: "
        "its volume name is not in the record"
    )
    assert stub.calls == []


def test_the_slot_documents_are_scoped_by_the_agent(
    tmp_path: Path, install_stub
) -> None:
    """The brief's landmine: the policy carries the egress-proxy credentials."""
    from envd_service.route_b import W1SlotPool

    document = tmp_path / "10007" / f"rb-{SANDBOX}" / "policy.json"
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
    from envd_service.route_b import W1SlotPool

    root = tmp_path / "slots"
    document = root / "10007" / f"rb-{SANDBOX}" / "policy.json"
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
            f"rb-{SANDBOX}",
            SANDBOX,
        )
    assert str(excinfo.value) == "the control plane refused it"
    assert oct(document.stat().st_mode & 0o777) == "0o600"
