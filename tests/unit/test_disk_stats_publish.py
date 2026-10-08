"""SEC-K0S-006: the worker publishes each sandbox's disk accounting.

`statfs(2)` is not namespaced -- it answers with the host volume -- so the
sandbox is shown the platform's own numbers instead: the quota it was sold and
what is left of it. The fork reads a file the worker writes; these pin the
worker's half (where the file goes, what it contains, and that the agent
refreshes it from the same measurement the ledger uses).
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from envd_service.agent import _write_disk_stats
from envd_service.config import Settings
from gateway_common.paths import sandbox_disk_stats_path, sandbox_runtime_dir

#: The uid the own-identity slot runs as -- never the writer's. The lane's pool hands
#: uids out from here (``tests/security/conftest.SANDBOX_UID``).
SLOT_UID = 1000


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


def test_only_the_ledger_is_world_readable_in_the_runtime_dir(tmp_path):
    """The reader is the own-identity slot at *another* uid, not the writer.

    In the deployed shape the worker writes as 65534 and the slot that reads
    the accounting file runs at the sandbox's own host uid, so the directory
    chain has to be traversable by name and the file readable -- while the two
    files beside it (the record with its access token, the command log with the
    sandbox's own output) stay closed.

    Measured 2026-10-01: with the runtime directory at its historical ``0700``
    the slot's read raised EACCES and every ``statfs`` in the sandbox silently
    answered with the node's volume.
    """
    from envd_service.process.logs import CommandLogWriter
    from envd_service.runtime.registry import RuntimeRegistry

    settings = _settings(tmp_path)
    registry = RuntimeRegistry(settings.workspace_base, state_base=settings.state_base)
    registry.register(
        sandbox_id="sbx_abc",
        access_token="tok",
        workspace_dir=str(settings.workspace_base / "sbx_abc"),
        disk_mb=1024,
    )
    _write_disk_stats(settings, "sbx_abc", total_bytes=10 * 1024**3, used_bytes=4 * 1024**3)

    runtime_dir = sandbox_runtime_dir(
        settings.workspace_base, "sbx_abc", state_base=settings.state_base
    )
    logs = CommandLogWriter(runtime_dir)
    logs.start(1, ["echo", "hi"])
    logs.write(1, "stdout", b"hi\n")

    assert stat.S_IMODE(runtime_dir.parent.stat().st_mode) == 0o711
    assert stat.S_IMODE(runtime_dir.stat().st_mode) == 0o711
    modes = {
        entry.name: stat.S_IMODE(entry.stat().st_mode)
        for entry in runtime_dir.iterdir()
    }
    assert modes["disk-stats"] == 0o644, modes
    assert modes["sandbox.json"] == 0o600, modes
    assert modes["command-logs.jsonl"] == 0o600, modes
    # ...and the invariant that keeps a future file from becoming readable by
    # accident, now that the directory is traversable:
    world_readable = sorted(
        name for name, mode in modes.items() if mode & 0o004 and name != "disk-stats"
    )
    assert world_readable == [], (
        "only the disk accounting is meant to be world-readable inside "
        f"_runtime/<id>: {world_readable}"
    )


@pytest.mark.skipif(os.geteuid() != 0, reason="dropping to another uid needs root")
def test_a_foreign_uid_reads_the_ledger_and_not_the_record(tmp_path):
    """What the slot actually does: read it as a uid that is not the owner."""
    settings = _settings(tmp_path)
    _write_disk_stats(settings, "sbx_abc", total_bytes=10 * 1024**3, used_bytes=0)
    runtime_dir = sandbox_runtime_dir(
        settings.workspace_base, "sbx_abc", state_base=settings.state_base
    )
    ledger = runtime_dir / "disk-stats"
    record = runtime_dir / "sandbox.json"
    record.write_text('{"accessToken": "secret"}')
    os.chmod(record, 0o600)

    # pytest's own tmp_path chain is 0700 root-owned, and a slot would be
    # stopped by those *ancestors* before it ever reached the modes under test.
    # Widen every level that has no "other" execute bit, from the ledger
    # upwards (``tests.security.conftest.make_sandbox_visible`` stops at the
    # first already-visible level, which for a 0755 workspace base is the
    # directory we still need to fix).
    candidate = ledger.parent
    while candidate != candidate.parent:
        try:
            mode = candidate.stat().st_mode
        except OSError:
            break
        if not mode & 0o001:
            os.chmod(candidate, mode | 0o055)
        candidate = candidate.parent

    probe = (
        "import sys\n"
        "for label, path in (('ledger', sys.argv[1]), ('record', sys.argv[2])):\n"
        "    try:\n"
        "        with open(path) as handle:\n"
        "            print(label, 'OK', handle.read().strip())\n"
        "    except OSError as exc:\n"
        "        print(label, 'ERR', exc.errno)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", probe, str(ledger), str(record)],
        user=SLOT_UID,
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr
    assert f"ledger OK {10 * 1024**3} 0" in done.stdout, done.stdout
    assert f"record ERR {errno.EACCES}" in done.stdout, done.stdout
