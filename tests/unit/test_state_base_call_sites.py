"""``E2B_STATE_BASE`` on the worker's side: settings and every call site (N27).

N27 Task 1 gave the four path helpers a ``state_base`` keyword that defaults to
today's layout (``gateway_common/paths.py``). This file pins the half that
*uses* it: the worker reads the base once (``Settings.state_base``) and every
platform path -- the runtime record, the command log, the checkpoint images,
the uid pool's lock and reservations -- goes through it.

The load-bearing case is ``SandboxRuntimeContext``: its old code derived the
base from a path the *record* claimed (``Path(record.workspace_dir).parent``),
which is wrong the moment the tree root sinks one level
(``<export>/workspaces/<id>`` + ``<export>/state``) -- that path is the tree
root, not the base the platform's own files live under. The pin is a behaviour
plus a source count, not a sentence in a plan.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

from envd_service import xfs_quota
from envd_service.agent import _scan_workspace_runtimes
from envd_service.config import Settings
from envd_service.runtime.context import SandboxRuntimeContext
from envd_service.runtime.registry import RuntimeRegistry, RuntimeSandbox
from envd_service.uid_pool import UidPool


def _clean_bases(monkeypatch) -> None:
    """Neither base may come from the machine running the tests."""
    monkeypatch.delenv("E2B_STATE_BASE", raising=False)
    monkeypatch.delenv("E2B_WORKSPACE_BASE", raising=False)


def test_settings_state_base_defaults_to_workspace_base(monkeypatch) -> None:
    _clean_bases(monkeypatch)
    s = Settings(workspace_base=Path("/ws"))
    assert s.state_base == Path("/ws")


def test_settings_state_base_reads_the_env(monkeypatch) -> None:
    monkeypatch.setenv("E2B_STATE_BASE", "/st")
    s = Settings(workspace_base=Path("/ws"))
    assert s.state_base == Path("/st")


def test_settings_state_base_follows_the_base_this_object_was_given(
    monkeypatch,
) -> None:
    """A second base only ever comes from ``E2B_STATE_BASE``.

    Not from ``E2B_WORKSPACE_BASE``: a caller that passes ``workspace_base``
    explicitly (a test, an embedder, the combined single-process deployment)
    must not end up with the platform's files under some *other* base that the
    environment happened to name.
    """
    monkeypatch.delenv("E2B_STATE_BASE", raising=False)
    monkeypatch.setenv("E2B_WORKSPACE_BASE", "/elsewhere")
    s = Settings(workspace_base=Path("/ws"))
    assert s.state_base == Path("/ws")


def test_uid_pool_lock_and_reservations_follow_the_state_base() -> None:
    pool = UidPool(Path("/ws"), state_base=Path("/st"), start=10000, size=10)
    assert pool.lock_path == Path("/st/.uid_pool.lock")


def test_uid_pool_writes_its_marker_under_the_state_base(tmp_path: Path) -> None:
    """The reservation marker is platform state, so it moves with the lock."""
    workspace = tmp_path / "workspaces"
    state = tmp_path / "state"
    state.mkdir()
    pool = UidPool(workspace, state_base=state, start=10000, size=10)

    uid = pool.acquire("sbx_a")

    assert uid == 10000
    assert (state / ".uid_reservations" / "sbx_a").read_text(
        encoding="utf-8"
    ) == "10000\n"
    assert (workspace / ".uid_reservations").exists() is False


def test_the_registry_writes_records_under_the_state_base(tmp_path: Path) -> None:
    workspace = tmp_path / "workspaces"
    state = tmp_path / "state"
    workspace.mkdir()
    registry = RuntimeRegistry(workspace, state_base=state)

    registry.register(
        sandbox_id="sbx_a",
        access_token="tok",
        workspace_dir=str(workspace / "sbx_a"),
    )

    assert (state / "_runtime" / "sbx_a" / "sandbox.json").is_file() is True
    assert (workspace / "_runtime").exists() is False
    assert registry.get("sbx_a").sandbox_id == "sbx_a"


def test_the_orphan_scan_does_not_read_a_platform_namespace_as_a_tree(
    tmp_path: Path,
) -> None:
    """The state base's own directory is never an unreadable sandbox tree.

    The transitional config keeps the old workspace base for a while and has
    ``state/`` created under it; ``state`` spells a legal sandbox id, so the
    shape rule alone reads the platform's whole tree as one sandbox tree and
    the reconcile round reports it as ``unmaterialised`` on every pass. It is
    reported, never deleted -- noise, not data loss -- but the name is reserved
    (``gateway_common.paths.STATE_DIR_NAME``) and the scan has to honour it.
    """
    workspace = tmp_path / "workspaces"
    (workspace / "state" / "_runtime" / "sbx_live").mkdir(parents=True)
    (workspace / "sbx_live").mkdir()
    registry = RuntimeRegistry(workspace)
    registry.register(
        sandbox_id="sbx_live",
        access_token="tok",
        workspace_dir=str(workspace / "sbx_live"),
    )

    records, unmaterialised = _scan_workspace_runtimes(
        Settings(executor="local", workspace_base=workspace), registry
    )

    assert unmaterialised == []
    assert sorted(records) == ["sbx_live"]


def test_a_reserved_name_that_carries_a_record_is_still_a_tree(
    tmp_path: Path,
) -> None:
    """The reserved table suppresses a *report*, never a record (M1).

    ``state`` is a legal sandbox id, and the leak the M1 review found was
    exactly a name filter dropping a live client-chosen tree out of the scans.
    So the scan reads the record first: a tree that really carries a reserved
    name keeps being materialised, and only the nameless platform directory is
    passed over.
    """
    workspace = tmp_path / "workspaces"
    (workspace / "state").mkdir(parents=True)
    registry = RuntimeRegistry(workspace)
    registry.register(
        sandbox_id="state",
        access_token="tok",
        workspace_dir=str(workspace / "state"),
    )

    records, unmaterialised = _scan_workspace_runtimes(
        Settings(executor="local", workspace_base=workspace), registry
    )

    assert unmaterialised == []
    assert sorted(records) == ["state"]


def test_the_recorded_project_ids_are_read_from_the_state_base(
    tmp_path: Path,
) -> None:
    """The quota reconciler's "still live" set is the platform record set.

    Read from the workspace base it is empty under the committed shape, and an
    empty "recorded" set is the fail-*open* direction: every live project row
    looks like an orphan and its directory's project state is cleared, which
    takes the sandbox's quota enforcement away for good.
    """
    workspace = tmp_path / "workspaces"
    state = tmp_path / "state"
    (workspace / "sbx_a").mkdir(parents=True)
    record = state / "_runtime" / "sbx_a"
    record.mkdir(parents=True)
    (record / "sandbox.json").write_text(
        json.dumps({"sandbox_id": "sbx_a", "project_id": 700}), encoding="utf-8"
    )

    assert xfs_quota._recorded_projids(workspace) == set()
    assert xfs_quota._recorded_projids(workspace, state_base=state) == {700}


def test_the_command_log_follows_the_state_base(tmp_path: Path) -> None:
    """The writer's directory is the platform's, not the record's tree.

    The old line derived the base from a path the *record* claimed
    (``Path(record.workspace_dir).parent``). That is the tree's own parent, so
    under the committed shape it is the workspace base -- the one place the
    command log is *not* allowed to be, since the platform's files moved to the
    state base (N27).
    """
    workspace = tmp_path / "workspaces"
    state = tmp_path / "state"
    record = RuntimeSandbox(
        sandbox_id="sbx_log",
        access_token="tok",
        workspace_dir=str(workspace / "sbx_log"),
    )
    ctx = SandboxRuntimeContext(
        record,
        Settings(executor="local", workspace_base=workspace),
        runtime_registry=RuntimeRegistry(workspace, state_base=state),
    )

    assert ctx.command_logs._path == (
        state / "_runtime" / "sbx_log" / "command-logs.jsonl"
    )
    assert (
        inspect.getsource(SandboxRuntimeContext.__init__).count(
            "Path(record.workspace_dir).parent"
        )
        == 0
    )
