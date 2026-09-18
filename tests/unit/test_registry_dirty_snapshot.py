"""N25/L2c: the registry's report path, dirty-aware and never wrong.

`disk_usage_snapshot(dirty=True)` may answer from the incremental ledger, but
every path that cannot answer honestly -- no provider, a provider that does
not know the sandbox, an overflowed dirty set, an unreadable directory -- has
to end in the whole-tree walk, because the number decides whether a sandbox
gets paused. The equality is asserted against `priv_helpers.dir_size`, the
walk it replaces, not against itself.
"""

from __future__ import annotations

from pathlib import Path

from envd_service.priv_helpers import dir_size
from envd_service.runtime.registry import RuntimeRegistry


def _tree(base: Path, sandbox_id: str) -> Path:
    tree = base / sandbox_id
    (tree / "workspace").mkdir(parents=True)
    (tree / "workspace" / "a.txt").write_bytes(b"a" * 100)
    (tree / "workspace" / "deep").mkdir()
    (tree / "workspace" / "deep" / "b.txt").write_bytes(b"b" * 900)
    return tree


def _registry(base: Path, sandbox_id: str) -> RuntimeRegistry:
    registry = RuntimeRegistry(base)
    registry.register(
        sandbox_id=sandbox_id,
        access_token="t",
        workspace_dir=str(base / sandbox_id),
    )
    return registry


def test_without_a_provider_the_number_is_the_walk(tmp_path):
    registry = _registry(tmp_path, "sbx_plain")
    tree = _tree(tmp_path, "sbx_plain")

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_plain": dir_size(tree)}


def test_a_dirty_report_matches_the_walk(tmp_path):
    registry = _registry(tmp_path, "sbx_dirty")
    tree = _tree(tmp_path, "sbx_dirty")
    # The provider answers exactly what the mediator would: the directory that
    # contains the change.
    updated = tree / "workspace" / "deep"
    registry.set_dirty_provider(
        lambda sandbox_id: ([str(updated)], False)
        if sandbox_id == "sbx_dirty"
        else None
    )

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_dirty": 1000}

    (tree / "workspace" / "deep" / "c.txt").write_bytes(b"c" * 7)
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_dirty": 1007}
    assert registry.disk_usage_snapshot(dirty=True)["sbx_dirty"] == dir_size(tree)


def test_an_unknown_sandbox_falls_back_to_the_walk(tmp_path):
    """A provider that cannot answer is 'walk it', never 'zero'."""
    registry = _registry(tmp_path, "sbx_unknown")
    tree = _tree(tmp_path, "sbx_unknown")
    registry.set_dirty_provider(lambda sandbox_id: None)

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_unknown": dir_size(tree)}


def test_overflow_rebuilds_instead_of_trusting_the_set(tmp_path):
    registry = _registry(tmp_path, "sbx_overflow")
    tree = _tree(tmp_path, "sbx_overflow")
    registry.set_dirty_provider(lambda sandbox_id: ([], True))

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_overflow": dir_size(tree)}

    # ...and the ledger is usable again afterwards.
    (tree / "workspace" / "d.txt").write_bytes(b"d" * 3)
    registry.set_dirty_provider(lambda sandbox_id: ([str(tree / "workspace")], False))
    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_overflow": dir_size(tree)}


def test_a_worker_written_file_is_marked_at_the_write_point(tmp_path):
    """The MCP token is the one platform write inside the tree (N25/L2c)."""
    registry = _registry(tmp_path, "sbx_token")
    tree = _tree(tmp_path, "sbx_token")
    registry.set_dirty_provider(lambda sandbox_id: ([], False))
    registry.disk_usage_snapshot(dirty=True)  # build the baseline first

    token_dir = tree / "etc" / "mcp-gateway"
    token_dir.mkdir(parents=True)
    (token_dir / ".token").write_text("secret", encoding="utf-8")
    registry.note_local_write("sbx_token", token_dir)

    snapshot = registry.disk_usage_snapshot(dirty=True)
    assert snapshot == {"sbx_token": dir_size(tree)}


def test_a_recreated_id_does_not_inherit_the_old_baseline(tmp_path):
    registry = _registry(tmp_path, "sbx_reuse")
    tree = _tree(tmp_path, "sbx_reuse")
    registry.set_dirty_provider(lambda sandbox_id: ([], False))
    registry.disk_usage_snapshot(dirty=True)

    (tree / "workspace" / "e.txt").write_bytes(b"e" * 11)
    registry.unregister("sbx_reuse")
    registry.register(
        sandbox_id="sbx_reuse", access_token="t", workspace_dir=str(tree)
    )

    assert registry.disk_usage_snapshot(dirty=True) == {"sbx_reuse": dir_size(tree)}
