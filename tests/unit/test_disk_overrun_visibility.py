"""N25: over budget is *visible*, and optionally pinned.

Two halves of the same gap. The control plane used to have a log line per
crossing and nothing else, so "how many sandboxes are over, and by how much"
was not answerable from the fleet view; and the worker's gate is computed from
an estimate, so an estimate that dips back under the budget on a stale walk
would re-open writes to a tree that is still over.
"""

from __future__ import annotations

from pathlib import Path

from control_plane.config import Settings
from control_plane.registry.manager import SandboxRegistry

from envd_service.runtime.registry import RuntimeRegistry


def _cp_settings(**overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=100,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _worker_registry(base: Path, sandbox_id: str, *, disk_mb: int) -> RuntimeRegistry:
    registry = RuntimeRegistry(base)
    registry.register(
        sandbox_id=sandbox_id,
        access_token="t",
        workspace_dir=str(base / sandbox_id),
        disk_mb=disk_mb,
    )
    (base / sandbox_id / "workspace").mkdir(parents=True, exist_ok=True)
    (base / sandbox_id / "workspace" / "a.bin").write_bytes(b"a" * 1000)
    registry.set_dirty_provider(
        lambda sid: ([str(base / sandbox_id / "workspace")], False)
    )
    registry._tighten_interval_s = 0.0
    return registry


def _recorder():
    calls: list[tuple[str, int]] = []

    def tightener(sandbox_id: str, bytes_: int, stamps=None):
        calls.append((sandbox_id, bytes_))
        return {"applied_bytes": bytes_}

    return calls, tightener


def test_the_fleet_view_counts_who_is_over_and_by_how_much(tmp_path):
    registry = SandboxRegistry(_cp_settings())
    over = registry.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
        sandbox_id="sbx_over",
    )
    fits = registry.create(
        template_id="base",
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
        sandbox_id="sbx_fits",
    )

    budget = over.disk_size_mb * 1024 * 1024
    overran = registry.enforce_disk_budget(
        {"sbx_over": budget + 3 * 1024 * 1024, "sbx_fits": 1024}
    )
    assert [r.sandbox_id for r in overran] == ["sbx_over"]
    assert registry.disk_overrun_stats() == {"sandboxes": 1, "overMB": 3}

    # Back inside: the count drops without anyone having to clear it by hand.
    assert registry.enforce_disk_budget({"sbx_over": 1024, "sbx_fits": 1024}) == []
    assert registry.disk_overrun_stats() == {"sandboxes": 0, "overMB": 0}

    # And a sandbox that stops being reported (killed) does not linger.
    registry.enforce_disk_budget({"sbx_over": budget + 1024 * 1024})
    assert registry.disk_overrun_stats()["sandboxes"] == 1
    registry.enforce_disk_budget({"sbx_fits": 1024})
    assert registry.disk_overrun_stats() == {"sandboxes": 0, "overMB": 0}
    assert fits.sandbox_id == "sbx_fits"


def test_log_is_the_default_and_does_not_pin(tmp_path):
    registry = _worker_registry(tmp_path, "sbx_log", disk_mb=1)
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    record = registry.get("sbx_log")
    budget = 1024 * 1024

    # A crossing: the gate goes to zero (that is the existing behaviour).
    registry._note_budget_crossing(record, budget + 1024 * 1024)
    registry._maybe_tighten(record, budget + 1024 * 1024)
    assert calls == [("sbx_log", 0)]

    # A later walk that wobbles back under the budget re-opens the gate,
    # because nothing was pinned: `log` changes no behaviour.
    registry._note_budget_crossing(record, int(budget * 0.99))
    registry._maybe_tighten(record, int(budget * 0.99))
    assert calls[-1][0] == "sbx_log"
    assert calls[-1][1] == budget - int(budget * 0.99)


def test_deny_pins_the_gate_shut_across_a_wobble(tmp_path):
    registry = _worker_registry(tmp_path, "sbx_deny", disk_mb=1)
    registry._overrun_action = "deny"
    registry._overrun_deny_s = 60.0
    calls, tightener = _recorder()
    registry.set_disk_tightener(tightener)
    record = registry.get("sbx_deny")
    budget = 1024 * 1024

    registry._note_budget_crossing(record, budget + 1024 * 1024)
    registry._maybe_tighten(record, budget + 1024 * 1024)
    assert calls == [("sbx_deny", 0)]

    # The wobble that `log` would honour: still zero, because the crossing is
    # inside the deny window and the measurement has not clearly come back in.
    registry._note_budget_crossing(record, int(budget * 0.99))
    registry._maybe_tighten(record, int(budget * 0.99))
    assert calls == [("sbx_deny", 0)], "a pinned gate must not re-open"

    # Clearly back inside (the hysteresis): the pin lets go and the sandbox can
    # write again -- the deletes it needed still work, which is the whole point
    # of refusing instead of freezing.
    registry._note_budget_crossing(record, int(budget * 0.5))
    registry._maybe_tighten(record, int(budget * 0.5))
    assert calls[-1][1] == budget - int(budget * 0.5)
