"""SEC-K0S-006: the worker publishes each sandbox's disk accounting.

`statfs(2)` is not namespaced -- it answers with the host volume -- so the
sandbox is shown the platform's own numbers instead: the quota it was sold and
what is left of it. The fork reads a file the worker writes; these pin the
worker's half (where the file goes, what it contains, and that the agent
refreshes it from the same measurement the ledger uses).
"""

from __future__ import annotations

from pathlib import Path

from envd_service.agent import _write_disk_stats
from envd_service.config import Settings
from gateway_common.paths import sandbox_disk_stats_path, sandbox_runtime_dir


def _settings(tmp_path: Path) -> Settings:
    return Settings(workspace_base=tmp_path / "trees")


def test_the_stats_file_lives_beside_the_sandbox_record(tmp_path):
    """Not in the sandbox's tree: the sandbox must not write its own numbers."""
    settings = _settings(tmp_path)
    path = sandbox_disk_stats_path(
        settings.workspace_base, "sbx_abc", state_base=settings.state_base
    )
    assert path == sandbox_runtime_dir(
        settings.workspace_base, "sbx_abc", state_base=settings.state_base
    ) / "disk-stats"
    assert path.parent.name == "sbx_abc"
    assert path.parent.parent.name == "_runtime"


def test_publishing_writes_exactly_the_two_byte_counts(tmp_path):
    """The wire format the fork parses: ``<total> <used>``."""
    settings = _settings(tmp_path)
    _write_disk_stats(settings, "sbx_abc", total_bytes=10 * 1024**3, used_bytes=4 * 1024**3)
    path = sandbox_disk_stats_path(
        settings.workspace_base, "sbx_abc", state_base=settings.state_base
    )
    assert path.read_text() == f"{10 * 1024**3} {4 * 1024**3}\n"


def test_publishing_creates_the_directory_and_is_idempotent(tmp_path):
    """A create can beat the registry's own mkdir; the publish must not care."""
    settings = _settings(tmp_path)
    path = sandbox_disk_stats_path(
        settings.workspace_base, "sbx_abc", state_base=settings.state_base
    )
    assert not path.parent.exists()
    _write_disk_stats(settings, "sbx_abc", total_bytes=1, used_bytes=0)
    assert path.read_text() == "1 0\n"
    _write_disk_stats(settings, "sbx_abc", total_bytes=1, used_bytes=1)
    assert path.read_text() == "1 1\n"


def test_publishing_never_raises_on_an_unwritable_target(tmp_path, monkeypatch):
    """Best effort: a failed publish must not fail a sandbox create."""
    settings = _settings(tmp_path)

    def _boom(*_args, **_kwargs):
        raise OSError("no space for the accounting file")

    monkeypatch.setattr("gateway_common.paths.write_text_atomically", _boom)
    _write_disk_stats(settings, "sbx_abc", total_bytes=1, used_bytes=0)
