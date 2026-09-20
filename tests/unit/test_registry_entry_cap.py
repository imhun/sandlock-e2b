"""N31: the registry tells the sandbox how many names its tree holds.

The byte budget cannot see a tree that grows by names -- an empty file costs
zero bytes, and a directory is not counted at all -- so this is the second
axis, and it has to behave like the first one: send on a material move or on
the *crossing*, stay quiet otherwise, and do nothing at all when the knob is
off.
"""

from __future__ import annotations

from pathlib import Path

from envd_service.runtime.registry import RuntimeRegistry


def _tree(base: Path, sandbox_id: str) -> Path:
    tree = base / sandbox_id
    (tree / "workspace").mkdir(parents=True)
    return tree


def _registry(base: Path, sandbox_id: str) -> RuntimeRegistry:
    registry = RuntimeRegistry(base)
    registry.register(
        sandbox_id=sandbox_id,
        access_token="t",
        workspace_dir=str(base / sandbox_id),
    )
    return registry


def test_the_entry_cap_is_off_by_default(tmp_path):
    registry = _registry(tmp_path, "sbx_off")
    _tree(tmp_path, "sbx_off")
    sent: list[tuple[str, int, int]] = []
    registry.set_entry_tightener(lambda sid, entries, limit: sent.append((sid, entries, limit)))
    record = registry.get("sbx_off")
    registry._maybe_tighten_entries(record, 10_000_000)
    assert sent == []


def test_the_entry_cap_sends_the_count_and_the_limit(tmp_path):
    registry = _registry(tmp_path, "sbx_cap")
    _tree(tmp_path, "sbx_cap")
    registry._max_entries = 100
    registry._entry_interval_s = 0.0
    sent: list[tuple[str, int, int]] = []
    registry.set_entry_tightener(lambda sid, entries, limit: sent.append((sid, entries, limit)))
    record = registry.get("sbx_cap")

    registry._maybe_tighten_entries(record, 10)
    assert sent == [("sbx_cap", 10, 100)]

    # A move smaller than the step is not worth a verb.
    registry._maybe_tighten_entries(record, 20)
    assert sent == [("sbx_cap", 10, 100)]

    # Crossing the cap always is, however small the move.
    registry._maybe_tighten_entries(record, 100)
    assert sent == [("sbx_cap", 10, 100), ("sbx_cap", 100, 100)]

    # And so is coming back under it: that is the sandbox deleting its way in.
    registry._maybe_tighten_entries(record, 99)
    assert sent == [
        ("sbx_cap", 10, 100),
        ("sbx_cap", 100, 100),
        ("sbx_cap", 99, 100),
    ]
