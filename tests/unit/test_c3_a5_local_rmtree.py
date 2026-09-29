"""C3 Task 4 / A5: the paired ``_runtime/<id>`` removal confirms, or fails.

The defect (``docs/c3-privilege-relocation.md`` §13.7): the sandbox tree went
through the *confirming* removal (``priv_helpers.remove_tree(..., on_error=
"raise")`` plus a look at the disk) while the platform's paired directory
``<state base>/_runtime/<id>`` was removed with ``shutil.rmtree(...,
ignore_errors=True)`` and then unconditionally answered "gone". For a CP that
is neither root nor the directory's owner -- which is what Task 5 makes it --
that is a silent half-delete that reads as success (measured on the cluster:
``returned=True survived=True``).

⚠ A5 lives in ``_destroy_local`` (the ``local://`` lane), and both production
stacks set ``E2B_ENABLE_LOCAL_NODE=false``: this is a **defect in dead code**,
fixed as a correctness matter, not a production leak. The cluster probe that
reproduced it (``deploy/scripts/acceptance/probe_c3_a5_silent_rmtree.py``) needs
root and another uid to drop to, so the host lane pins the behaviour *exactly*
instead: the paired removal goes through the same confirming path, and a
directory that survives it is a failure (``False``), never ``True``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.api import sandboxes as sandbox_api
from envd_service import priv_helpers

SANDBOX = "sbx_a5"


class _State:
    """The two attributes ``_remove_local_tree_confirming`` reads."""

    def __init__(self, workspace_base: Path) -> None:
        self.workspace_base = workspace_base / "workspaces"
        self.state_base = workspace_base / "state"
        self.workspace_base.mkdir(parents=True, exist_ok=True)
        self.state_base.mkdir(parents=True, exist_ok=True)

    def tree(self) -> Path:
        return self.workspace_base / SANDBOX

    def runtime_dir(self) -> Path:
        return self.state_base / "_runtime" / SANDBOX


def _broken_remove_tree(
    monkeypatch: pytest.MonkeyPatch, *, remove: tuple[Path, ...] = ()
) -> list[tuple[Path, str]]:
    """A removal that silently does nothing, like an EACCES swallowed by rmtree.

    ``remove`` names the paths it *does* delete, so a test can drive the case
    where the tree half really happens and the paired half does not.
    """
    import shutil

    calls: list[tuple[Path, str]] = []

    def _remove(path, *, on_error: str = "ignore") -> None:
        assert on_error == "raise", "no half of a teardown may fail silently"
        path = Path(path)
        calls.append((path, on_error))
        if path in remove:
            shutil.rmtree(path)

    monkeypatch.setattr(priv_helpers, "remove_tree", _remove)
    return calls


def test_a_surviving_runtime_dir_is_a_failed_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The A5 shape exactly: tree gone, paired directory still there."""
    state = _State(tmp_path)
    runtime_dir = state.runtime_dir()
    runtime_dir.mkdir(parents=True)
    (runtime_dir / "command-logs.jsonl").write_text("{}\n", encoding="utf-8")
    calls = _broken_remove_tree(monkeypatch, remove=(state.tree(),))

    assert sandbox_api._remove_local_tree_confirming(state, SANDBOX) is False
    assert calls == [
        (state.workspace_base / SANDBOX, "raise"),
        (runtime_dir, "raise"),
    ]
    assert runtime_dir.is_dir()


def test_the_paired_removal_goes_through_the_confirming_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both halves use one removal discipline -- that is the whole fix."""
    state = _State(tmp_path)
    state.tree().mkdir(parents=True)
    state.runtime_dir().mkdir(parents=True)
    # The tree half really happens; the paired half silently does not.
    calls = _broken_remove_tree(monkeypatch, remove=(state.tree(),))

    assert sandbox_api._remove_local_tree_confirming(state, SANDBOX) is False
    assert calls == [(state.tree(), "raise"), (state.runtime_dir(), "raise")]


def test_a_clean_teardown_confirms_both_halves(tmp_path: Path) -> None:
    state = _State(tmp_path)
    state.tree().mkdir(parents=True)
    state.runtime_dir().mkdir(parents=True)

    assert sandbox_api._remove_local_tree_confirming(state, SANDBOX) is True
    assert not state.tree().exists()
    assert not state.runtime_dir().exists()


def test_an_absent_tree_does_not_turn_the_paired_half_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``FileNotFoundError`` branch: the tree is already gone, the pair is not."""
    state = _State(tmp_path)
    state.runtime_dir().mkdir(parents=True)
    calls = _broken_remove_tree(monkeypatch)

    assert sandbox_api._remove_local_tree_confirming(state, SANDBOX) is False
    assert calls == [(state.tree(), "raise"), (state.runtime_dir(), "raise")]


def test_an_absent_pair_is_a_successful_teardown(tmp_path: Path) -> None:
    state = _State(tmp_path)
    assert sandbox_api._remove_local_tree_confirming(state, SANDBOX) is True
