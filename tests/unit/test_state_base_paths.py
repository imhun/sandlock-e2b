"""E2B_STATE_BASE: the base the platform's own files live under (N27).

The four helpers must be byte-for-byte what they are today when no state base
is given -- that is the whole switch -- and must follow the state base when one
is. ``legacy=True`` is the pre-split location *inside the sandbox's own tree*
and stays there regardless of the state base.
"""
from __future__ import annotations

from pathlib import Path

from gateway_common import paths
from gateway_common.paths import is_reserved_platform_namespace

WS = Path("/var/lib/e2b/workspaces")
ST = Path("/var/lib/e2b/state")


def test_state_base_env_name_is_stable():
    assert paths.STATE_BASE_ENV == "E2B_STATE_BASE"


def test_default_state_base_is_the_workspace_base():
    assert paths.resolve_state_base(WS) == WS
    assert paths.sandbox_runtime_dir(WS, "sbx_a") == WS / "_runtime" / "sbx_a"
    assert paths.sandbox_record_path(WS, "sbx_a") == WS / "_runtime" / "sbx_a" / "sandbox.json"
    assert paths.sandbox_command_log_path(WS, "sbx_a") == WS / "_runtime" / "sbx_a" / "command-logs.jsonl"
    assert paths.sandbox_checkpoint_dir(WS, "sbx_a") == WS / "_runtime" / ".checkpoints" / "sbx_a"


def test_explicit_state_base_moves_every_platform_file():
    assert paths.resolve_state_base(WS, ST) == ST
    assert paths.sandbox_runtime_dir(WS, "sbx_a", state_base=ST) == ST / "_runtime" / "sbx_a"
    assert paths.sandbox_record_path(WS, "sbx_a", state_base=ST) == ST / "_runtime" / "sbx_a" / "sandbox.json"
    assert paths.sandbox_command_log_path(WS, "sbx_a", state_base=ST) == ST / "_runtime" / "sbx_a" / "command-logs.jsonl"
    assert paths.sandbox_checkpoint_dir(WS, "sbx_a", state_base=ST) == ST / "_runtime" / ".checkpoints" / "sbx_a"


def test_legacy_paths_stay_in_the_sandbox_tree():
    assert paths.sandbox_record_path(WS, "sbx_a", legacy=True, state_base=ST) == WS / "sbx_a" / "sandbox.json"
    assert paths.sandbox_command_log_path(WS, "sbx_a", legacy=True, state_base=ST) == WS / "sbx_a" / "command-logs.jsonl"


def test_the_state_namespace_is_reserved_alongside_the_existing_ones():
    """The state base's own directory name is a platform namespace.

    In the committed shape the state base is a *sibling* of the tree root
    (``<export>/workspaces/<id>`` + ``<export>/state``), so this name never
    shows up under the workspace base. It still has to be reserved: the
    transitional config keeps the old base for a while, and under it a bare
    ``state/`` directory would spell a legal sandbox id (the same M1 shape the
    list exists for). Adding it must not drop the names already there.
    """
    assert paths.STATE_DIR_NAME == "state"
    assert is_reserved_platform_namespace(paths.STATE_DIR_NAME) is True
    # The names that were already reserved stay reserved.
    assert is_reserved_platform_namespace("_pure_rootfs") is True
    assert is_reserved_platform_namespace("_runtime") is True
    assert is_reserved_platform_namespace("_volumes") is True
