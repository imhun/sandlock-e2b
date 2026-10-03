"""The idle->pause sweep: a sandbox nobody is using gives its capacity back.

The decision is ``last_active_at`` measured by the worker (in-sandbox commands,
file access, proxied traffic, and CPU above ``E2B_CPU_ACTIVITY_PERCENT``), so a
sandbox that is working -- or that a client is holding a stream open on -- is
never a candidate. What is left is the shape this sweep exists for: a sandbox
nobody touched for ``E2B_IDLE_PAUSE_AFTER_S`` that is still holding admission
capacity and a frozen-on-demand session.

Every test uses a fixed clock: ``idle 301s`` has to be exactly ``301.0``, not
``301.0004`` -- a threshold assertion that drifts with wall-clock time is not
an assertion.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from control_plane.app import create_app
from control_plane.config import Settings
from control_plane.registry.manager import SandboxRecord
from control_plane.registry.nodes import NodeRegistry
from gateway_common.timeutil import utcnow

FIXED = datetime(2026, 10, 3, 12, 0, 0, tzinfo=timezone.utc)


def _record(
    sandbox_id: str = "sbx_a",
    *,
    state: str = "running",
    idle_s: float = 301,
    ends_in_s: float = 600,
    metadata: dict | None = None,
) -> SandboxRecord:
    return SandboxRecord(
        template_id="base",
        sandbox_id=sandbox_id,
        client_id="cli_a",
        state=state,
        started_at=FIXED - timedelta(seconds=idle_s),
        end_at=FIXED + timedelta(seconds=ends_in_s),
        last_active_at=FIXED - timedelta(seconds=idle_s),
        metadata=dict(metadata or {}),
    )


class _FakeRegistry:
    """``list(state_filter=[...])`` and nothing else -- the sweep's whole view."""

    def __init__(self, records) -> None:
        self.records = list(records)
        self.filters: list[tuple[str, ...]] = []

    def list(self, *, state_filter=None):
        self.filters.append(tuple(state_filter or ()))
        if state_filter is None:
            return list(self.records)
        return [r for r in self.records if r.state in state_filter]


class _Recorder:
    def __init__(self, *, fail_on: tuple[str, ...] = ()) -> None:
        self.calls: list[tuple[str, float]] = []
        self._fail_on = frozenset(fail_on)

    async def __call__(self, record, idle_s: float) -> str:
        if record.sandbox_id in self._fail_on:
            raise RuntimeError(f"worker refused {record.sandbox_id}")
        self.calls.append((record.sandbox_id, idle_s))
        return "paused"


def _sweeper(on_idle, *, after_s: float = 300, claim=None, interval_seconds: float = 15.0):
    from control_plane.registry.idle_pause import IdlePauseSweeper

    return IdlePauseSweeper(
        after_s=after_s,
        on_idle=on_idle,
        interval_seconds=interval_seconds,
        claim=claim,
        now=lambda: FIXED,
    )


def test_an_idle_running_sandbox_is_paused():
    from control_plane.registry.idle_pause import idle_candidates

    record = _record(idle_s=301)
    assert idle_candidates([record], after_s=300, now=FIXED) == [(record, 301.0)]


def test_a_busy_sandbox_is_not_paused():
    from control_plane.registry.idle_pause import idle_candidates

    assert idle_candidates([_record(idle_s=299)], after_s=300, now=FIXED) == []


def test_an_expired_record_is_left_to_the_ttl_sweep():
    from control_plane.registry.idle_pause import idle_candidates

    record = _record(idle_s=3600, ends_in_s=-1)
    assert idle_candidates([record], after_s=300, now=FIXED) == []


def test_a_record_that_dies_within_a_minute_is_left_alone():
    from control_plane.registry.idle_pause import idle_candidates

    record = _record(idle_s=400, ends_in_s=59)
    assert idle_candidates([record], after_s=300, now=FIXED) == []


def test_a_metadata_opt_out_is_skipped():
    from control_plane.registry.idle_pause import idle_candidates

    record = _record(idle_s=400, metadata={"e2b_pause_on_idle": "OFF"})
    assert idle_candidates([record], after_s=300, now=FIXED) == []


def test_only_running_records_are_candidates():
    registry = _FakeRegistry(
        [
            _record("sbx_run"),
            _record("sbx_paused", state="paused"),
            _record("sbx_orphan", state="orphaned"),
        ]
    )
    sweep = _sweeper(_Recorder())
    due = sweep.due(registry)

    assert [r.sandbox_id for r, _ in due] == ["sbx_run"]
    assert registry.filters == [("running",)]


def test_the_switch_off_starts_no_task_and_takes_no_claim():
    claims: list[str] = []
    sweep = _sweeper(_Recorder(), after_s=0, claim=lambda: claims.append("x") or True)
    registry = _FakeRegistry([_record(idle_s=9999)])

    assert sweep.enabled is False
    sweep.start(registry)
    assert sweep._task is None
    assert asyncio.run(sweep.run_round(registry)) == 0
    assert claims == []


def test_the_round_is_single_flight():
    calls: list[str] = []
    sweep = _sweeper(_Recorder(), claim=lambda: calls.append("x") or False)
    registry = _FakeRegistry([_record(idle_s=301)])

    assert asyncio.run(sweep.run_round(registry)) == 0
    assert calls == ["x"]
    assert registry.filters == []


def test_one_failing_record_does_not_stop_the_round(caplog):
    recorder = _Recorder(fail_on=("sbx_b",))
    sweep = _sweeper(recorder)
    registry = _FakeRegistry(
        [_record("sbx_a", idle_s=301), _record("sbx_b", idle_s=302), _record("sbx_c", idle_s=303)]
    )

    with caplog.at_level(logging.WARNING, logger="control_plane.registry.idle_pause"):
        paused = asyncio.run(sweep.run_round(registry))

    assert paused == 2
    assert recorder.calls == [("sbx_c", 303.0), ("sbx_a", 301.0)]
    assert [r.getMessage() for r in caplog.records] == [
        "idle pause: sandbox sbx_b failed to pause: worker refused sbx_b"
    ]


def test_the_oldest_idle_record_is_paused_first():
    recorder = _Recorder()
    sweep = _sweeper(recorder)
    registry = _FakeRegistry(
        [_record("sbx_young", idle_s=301), _record("sbx_old", idle_s=900)]
    )

    assert asyncio.run(sweep.run_round(registry)) == 2
    assert recorder.calls == [("sbx_old", 900.0), ("sbx_young", 301.0)]


# -- the app's own wiring -------------------------------------------------


def _app_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(
        api_keys=("local-key",),
        max_sandboxes=0,
        default_memory_mb=512,
        default_cpu_percent=100,
        default_disk_mb=0,
        default_max_processes=64,
        max_total_memory_mb=0,
        max_total_cpu_percent=0,
        max_total_disk_mb=0,
        max_total_processes=0,
        internal_api_key="internal-key",
        workspace_base=tmp_path,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _app_registry_record(app, sandbox_id: str, *, idle_s: float):
    record = app.state.registry.create(
        template_id="base",
        sandbox_id=sandbox_id,
        timeout=300,
        metadata={},
        env_vars={},
        secure=True,
        allow_internet_access=False,
        base_image=None,
    )
    record.last_active_at = utcnow() - timedelta(seconds=idle_s)
    app.state.registry.save(record)
    return record


@pytest.mark.asyncio
async def test_the_app_does_not_start_the_sweep_when_the_switch_is_off(
    tmp_path, monkeypatch
):
    from control_plane.registry.idle_pause import IDLE_PAUSE_ENV

    monkeypatch.delenv(IDLE_PAUSE_ENV, raising=False)
    app = create_app(
        settings=_app_settings(tmp_path),
        runtime_registry=SimpleNamespace(
            unregister=lambda sandbox_id: None, set_state=lambda *args: None
        ),
        nodes_registry=NodeRegistry(),
        workspace_base=tmp_path,
    )
    async with app.router.lifespan_context(app):
        assert app.state.idle_sweeper.enabled is False
        assert app.state.idle_sweeper._task is None
        registry = app.state.registry
        _app_registry_record(app, "sbx_untouched", idle_s=10_000)
        assert await app.state.idle_sweeper.run_round(registry) == 0
        assert registry.get("sbx_untouched").state == "running"


@pytest.mark.asyncio
async def test_the_app_wires_the_sweep_when_the_switch_is_on(tmp_path, monkeypatch):
    from control_plane.registry.idle_pause import IDLE_PAUSE_ENV

    monkeypatch.delenv(IDLE_PAUSE_ENV, raising=False)
    states: list[tuple[str, str]] = []
    app = create_app(
        settings=_app_settings(tmp_path, idle_pause_after_s=0.05),
        runtime_registry=SimpleNamespace(
            unregister=lambda sandbox_id: None,
            set_state=lambda sandbox_id, state: states.append((sandbox_id, state)),
        ),
        nodes_registry=NodeRegistry(),
        workspace_base=tmp_path,
    )
    async with app.router.lifespan_context(app):
        assert app.state.idle_sweeper.enabled is True
        assert app.state.idle_sweeper._task is not None
        registry = app.state.registry
        _app_registry_record(app, "sbx_idle", idle_s=400)
        assert await app.state.idle_sweeper.run_round(registry) == 1
        assert registry.get("sbx_idle").state == "paused"
        assert states == [("sbx_idle", "paused")]
