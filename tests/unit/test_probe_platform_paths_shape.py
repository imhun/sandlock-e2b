"""Task 1 of docs/superpowers/plans/2026-10-09-control-plane-uid-relocation.md."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

PROBE = (
    Path(__file__).resolve().parents[2]
    / "deploy/scripts/acceptance/probe_platform_paths_shape.py"
)


def _probe():
    spec = importlib.util.spec_from_file_location("probe_platform_paths_shape", PROBE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


EXPECTED = [
    "_builds",
    "_images",
    "_secrets",
    "_snapshots",
    "_templates",
    "_volumes",
    "state",
    "workspaces/_migrate",
]


def test_shape_reports_the_eight_platform_paths(tmp_path):
    (tmp_path / "state").mkdir()
    entries = _probe().platform_path_shape(tmp_path)
    assert [e["path"] for e in entries] == EXPECTED
    state = entries[6]
    assert state["mode"] == "0o755"
    assert state["uid"] == os.getuid()
    assert state["gid"] == os.getgid()


def test_a_missing_path_is_reported_not_raised(tmp_path):
    entries = _probe().platform_path_shape(tmp_path)
    assert entries[0] == {"path": "_builds", "mode": "missing", "uid": -1, "gid": -1}


def test_world_readable_follows_the_st_iroth_bit():
    probe = _probe()
    assert probe.mode_is_world_readable({"path": "state", "mode": "0o755", "uid": 0, "gid": 0}) is True
    assert probe.mode_is_world_readable({"path": "state", "mode": "0o750", "uid": 0, "gid": 0}) is False
    assert probe.mode_is_world_readable({"path": "state", "mode": "0o640", "uid": 0, "gid": 0}) is False
    assert probe.mode_is_world_readable({"path": "state", "mode": "0o644", "uid": 0, "gid": 0}) is True


def test_a_missing_path_is_not_world_readable():
    probe = _probe()
    entry = {"path": "_builds", "mode": "missing", "uid": -1, "gid": -1}
    assert probe.mode_is_world_readable(entry) is False
