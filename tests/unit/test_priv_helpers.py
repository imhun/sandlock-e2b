"""The worker's privileged file steps, at the Python layer (C3 / N52).

One shape performs them now: the per-node **agent**, asked through
``envd_service.agent_fileops`` as ``{sandbox_id, op}``. The file-capability
brokers this module used to own (``e2b-slot-spawn`` / ``e2b-maint``), the local
``exec`` transport and C1's ``socket`` daemon are retired (2026-09-30,
open-issues N52), so what is left to pin here is:

* the shape switch: ``E2B_PRIV_HELPER_TRANSPORT`` accepts ``auto``/``agent``
  and refuses the retired ``exec``/``socket`` **by name**;
* the in-process teardown/size scan the worker's own data plane relies on
  (``remove_tree`` / ``dir_size``); and
* the startup guards and disclosures that survive (the worker-identity guard,
  the "no privileged file-step path" line, and the E5.1 flag).

The agent's own half -- the argv and the refusals it hands ``e2b-maint`` -- is
pinned in ``tests/unit/test_c3_agent_fileops.py``; that binary lives only in
the agent image now.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from envd_service import agent_fileops, priv_helpers as ph


@pytest.fixture(autouse=True)
def _clean_shape(monkeypatch):
    """No test may leak the agent client or the transport switch."""
    monkeypatch.delenv(ph.TRANSPORT_ENV, raising=False)
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [None])


def _settings(tmp_path: Path, **overrides):
    from envd_service.config import Settings

    (tmp_path / "sandboxes").mkdir(exist_ok=True)
    fields = dict(
        workspace_base=tmp_path / "sandboxes",
        uid_pool_start=10000,
        uid_pool_size=1000,
        slot_tmp_root=tmp_path / "sandboxes" / ".route-b",
    )
    fields.update(overrides)
    return Settings(**fields)


# ------------------------------------------------------------ the walk answer


def test_walk_output_is_parsed_with_owner_and_size(tmp_path: Path) -> None:
    entry = ph.WalkEntry.parse("f 10002 10002 600 12 /var/lib/e2b-sandboxes/a/b.txt")
    assert entry == ph.WalkEntry(
        kind="f",
        uid=10002,
        gid=10002,
        mode=0o600,
        size=12,
        path="/var/lib/e2b-sandboxes/a/b.txt",
    )


# -------------------------------------------------------------- the switch


def test_an_unknown_transport_is_refused(monkeypatch) -> None:
    """The shape switch is a closed list."""
    monkeypatch.setenv(ph.TRANSPORT_ENV, "telepathy")
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph._transport_setting()
    assert str(excinfo.value) == (
        "E2B_PRIV_HELPER_TRANSPORT must be 'auto' or 'agent' (got 'telepathy')"
    )


@pytest.mark.parametrize(
    "value, expected",
    [("", "auto"), ("auto", "auto"), ("AUTO", "auto"), ("agent", "agent")],
)
def test_only_auto_and_agent_are_accepted(
    monkeypatch, value: str, expected: str
) -> None:
    monkeypatch.setenv(ph.TRANSPORT_ENV, value)
    assert ph._transport_setting() == expected


def test_the_default_transport_is_auto(monkeypatch) -> None:
    monkeypatch.delenv(ph.TRANSPORT_ENV, raising=False)
    assert ph._transport_setting() == "auto"


@pytest.mark.parametrize("value", ("exec", "socket"))
def test_the_retired_transports_are_refused_by_name(monkeypatch, value: str) -> None:
    """N52: the worker-side file-capability shape and C1's socket are gone.

    Naming one has to be a *refusal*, not a quiet resolution to some other
    shape -- a deployment that asked for a privileged path must be told it is
    gone, and that the shape lives in git if it is ever needed again.
    """
    monkeypatch.setenv(ph.TRANSPORT_ENV, value)
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph._transport_setting()
    message = str(excinfo.value)
    assert f"E2B_PRIV_HELPER_TRANSPORT='{value}' is retired" in message
    assert "served by the per-node agent now" in message
    assert "lives in git" in message


def test_configure_priv_helpers_refuses_a_retired_transport(monkeypatch, tmp_path) -> None:
    """The refusal happens on the startup path, not only in the helper."""
    monkeypatch.setenv(ph.TRANSPORT_ENV, "exec")
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.configure_priv_helpers(_settings(tmp_path))
    assert "retired" in str(excinfo.value)


def test_configure_priv_helpers_installs_the_agent_client(monkeypatch, tmp_path) -> None:
    """``agent`` wires ``agent_fileops`` and returns None (nothing local)."""
    monkeypatch.setenv(ph.TRANSPORT_ENV, "agent")
    monkeypatch.setenv("E2B_CONTROL_PLANE_URL", "http://control-plane:8000")
    monkeypatch.setenv("E2B_NODE_ID", "node-1")
    settings = _settings(tmp_path, internal_api_key="internal-key")

    assert ph.configure_priv_helpers(settings) is None
    client = agent_fileops.active()
    assert client is not None
    assert ph.file_steps_available(settings) is True


def test_configure_priv_helpers_leaves_an_unknown_shape_alone(
    monkeypatch, tmp_path
) -> None:
    """Neither shape named: whatever a lane installed stays installed."""
    sentinel = object()
    monkeypatch.setattr(agent_fileops, "_ACTIVE", [sentinel])
    assert ph.configure_priv_helpers(_settings(tmp_path)) is None
    assert agent_fileops.active() is sentinel


def test_a_worker_with_no_agent_keeps_the_in_process_shape_and_says_so(
    tmp_path: Path, monkeypatch
) -> None:
    """The one warning a non-root, agent-less worker gets (N52).

    It used to name the missing file-capability binary; there is no binary to
    ship any more -- the agent is the only privileged path -- so the line names
    the model the worker is running instead.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 65534)
    assert ph.file_steps_available(_settings(tmp_path)) is False
    reason = ph.helpers_unavailable_reason(_settings(tmp_path))
    assert reason is not None
    assert "no privileged file-step path" in reason
    assert "E5.1" in reason


def test_a_root_worker_has_no_unavailable_reason(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ph.helpers_unavailable_reason(_settings(tmp_path)) is None


def test_root_is_its_own_file_step_path(tmp_path: Path, monkeypatch) -> None:
    """``file_steps_available`` answers about the *agent* only."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert ph.file_steps_available(_settings(tmp_path)) is False


# ------------------------------------------------------------------- guards


def test_the_workspace_mode_is_the_c1_shape() -> None:
    assert ph.WORKSPACE_MODE == 0o770


@pytest.mark.parametrize(
    "uid, gid, expected",
    [
        pytest.param(65534, 65534, None, id="outside-pool"),
        pytest.param(9999, 65534, None, id="just-below-the-pool"),
        pytest.param(11000, 65534, None, id="just-above-the-pool"),
        pytest.param(
            10000,
            65534,
            "the sandbox uid pool 10000..10999 contains the worker's own uid "
            "(10000): a sandbox would share the worker's identity and could "
            "read every other sandbox's 0770 workspace (the group model relies "
            "on the sandbox gid differing from the worker's); move "
            "E2B_UID_POOL_START/SIZE off 10000",
            id="worker-uid-in-pool",
        ),
        pytest.param(
            65534,
            10999,
            "the sandbox gid pool 10000..10999 contains the worker's own gid "
            "(10999): a sandbox would share the worker's identity and could "
            "read every other sandbox's 0770 workspace (the group model relies "
            "on the sandbox gid differing from the worker's); move "
            "E2B_UID_POOL_START/SIZE off 10999",
            id="worker-gid-in-pool",
        ),
    ],
)
def test_the_worker_identity_must_stay_outside_the_pool(
    uid: int, gid: int, expected: str | None
) -> None:
    """Fix round 1 (c1) guard: a pooled worker identity would defeat `0770`.

    A sandbox allocated the worker's own uid/gid would be inside the worker's
    trust boundary and could read every other sandbox's workspace, so the
    worker refuses to start unless it is named out of the pool.
    """
    if expected is None:
        ph.check_worker_identity_outside_pool(
            uid=uid, gid=gid, start=10000, size=1000
        )
        return
    with pytest.raises(ph.PrivHelperError) as excinfo:
        ph.check_worker_identity_outside_pool(
            uid=uid, gid=gid, start=10000, size=1000
        )
    assert str(excinfo.value) == expected


def test_create_app_refuses_a_pool_that_contains_the_worker_identity(
    tmp_path: Path, monkeypatch
) -> None:
    """The guard is wired into startup for **every** worker shape (a root
    worker's group model is just as load-bearing), so a bad pool fails the
    worker with the reason instead of quietly shipping the hole."""
    from envd_service.app import create_app
    from envd_service.runtime.registry import RuntimeRegistry

    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "getegid", lambda: 10050)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    settings = _settings(
        tmp_path,
        workspace_base=workspace,
        uid_pool_start=10000,
        uid_pool_size=1000,
    )
    with pytest.raises(ph.PrivHelperError) as excinfo:
        create_app(settings=settings, runtime_registry=RuntimeRegistry(workspace))
    assert str(excinfo.value) == (
        "the sandbox gid pool 10000..10999 contains the worker's own gid "
        "(10050): a sandbox would share the worker's identity and could read "
        "every other sandbox's 0770 workspace (the group model relies on the "
        "sandbox gid differing from the worker's); move "
        "E2B_UID_POOL_START/SIZE off 10050"
    )


# ---------------------------------------------------- the in-process steps


def test_remove_and_modes_are_in_process_first(tmp_path: Path, monkeypatch) -> None:
    """The worker's own group access handles an ordinary tree.

    There is no broker to fall back to any more (N52), so ``remove_tree`` is
    the worker's own ``rmtree`` and ``dir_size`` the worker's own walk -- both
    driven through the tree's ``0770 group=<worker gid>`` permission.
    """
    tree = tmp_path / "sandboxes" / "sbx_a"
    (tree / "workspace").mkdir(parents=True)
    (tree / "workspace" / "a.txt").write_bytes(b"0123456789")
    # N31 fix 2: the 10 file bytes plus each directory's own allocated size,
    # asserted against an `os.stat` oracle rather than the implementation's own
    # probe.
    assert ph.dir_size(tree) == (
        10
        + os.stat(tree).st_blocks * 512
        + os.stat(tree / "workspace").st_blocks * 512
    )
    ph.remove_tree(tree)
    assert not tree.exists()


def test_remove_tree_is_quiet_by_default_when_the_tree_cannot_be_removed(
    tmp_path: Path, monkeypatch
) -> None:
    """``on_error="ignore"`` is the background path: nothing to raise into."""
    tree = tmp_path / "sandboxes" / "sbx_sealed"
    (tree / "workspace").mkdir(parents=True)

    def deny(*args, **kwargs) -> None:
        raise PermissionError(13, "Permission denied", str(tree))

    monkeypatch.setattr(shutil, "rmtree", deny)
    ph.remove_tree(tree)
    assert tree.is_dir()


def test_a_missing_tree_is_not_an_error(tmp_path: Path) -> None:
    """Teardown runs twice on the same tree all the time (delete + GC)."""
    ph.remove_tree(tmp_path / "sandboxes" / "never-existed", on_error="raise")


def test_remove_tree_raises_when_the_tree_cannot_be_removed(
    tmp_path: Path, monkeypatch
) -> None:
    """W7-2's confirmation needs the failure to reach the caller.

    ``on_error="raise"`` is what the teardown paths use: a tree that cannot be
    removed must surface as a failure instead of a silent partial delete that
    leaves a tree with no readable record behind.
    """
    tree = tmp_path / "sandboxes" / "sbx_sealed"
    (tree / "workspace").mkdir(parents=True)

    def deny(*args, **kwargs) -> None:
        raise PermissionError(13, "Permission denied", str(tree))

    monkeypatch.setattr(shutil, "rmtree", deny)

    with pytest.raises(PermissionError) as excinfo:
        ph.remove_tree(tree, on_error="raise")

    assert str(excinfo.value) == f"[Errno 13] Permission denied: '{tree}'"
    assert tree.is_dir()


def test_dir_size_is_unknown_for_a_tree_the_worker_cannot_walk(
    tmp_path: Path, monkeypatch
) -> None:
    """``None`` is "unknown" and must never be read as "empty" (N31)."""
    tree = tmp_path / "sandboxes" / "sbx_sealed"
    (tree / "workspace").mkdir(parents=True)
    (tree / "workspace" / "a.txt").write_bytes(b"0123456789")

    def deny(*args, **kwargs) -> None:
        raise PermissionError(13, "Permission denied", str(tree))

    monkeypatch.setattr(os, "walk", deny)
    assert ph.dir_size(tree) is None
