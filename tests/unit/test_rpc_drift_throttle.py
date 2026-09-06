"""RPC drift-apply WARNING throttling (FUP #9).

An unreconciled record/instance drift repeats on every command RPC; the
worker must not spam the same WARNING. One warning per sandbox per
``DRIFT_WARN_THROTTLE_SECONDS`` window, with the exact log text preserved.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import envd_service.rpc as rpc
from gateway_common.network import NetworkUpdateConflictError


class _DriftingCtx:
    """Live context whose runtime copy differs from the record and cannot
    express the drifted update."""

    _network = {"allowInternetAccess": False}

    def update_network(self, network):
        raise NetworkUpdateConflictError("egress model flip")


def _request_with(sandbox_id: str, ctx):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                runtimes={sandbox_id: ctx},
                context_factory=None,
            )
        )
    )


def _runtime(sandbox_id: str):
    return SimpleNamespace(
        sandbox_id=sandbox_id,
        network={"allowInternetAccess": True},
    )


@pytest.fixture(autouse=True)
def _isolated_throttle(monkeypatch):
    """Reset the module throttle state and pin the injectable clock."""
    rpc._drift_warned_at.clear()
    ticks = [0.0]
    monkeypatch.setattr(rpc, "_drift_warn_clock", lambda: ticks[0])
    return ticks


def test_drift_failures_within_window_log_one_warning(caplog):
    sandbox_id = "sbx_drift_1"
    request = _request_with(sandbox_id, _DriftingCtx())
    runtime = _runtime(sandbox_id)
    caplog.set_level(logging.WARNING, logger="envd_service.rpc")
    expected = (
        "drift network update for sandbox sbx_drift_1 is not expressible "
        "on the live instance (egress model flip); keeping runtime policy"
    )

    assert rpc._context(request, runtime) is request.app.state.runtimes[sandbox_id]
    assert rpc._context(request, runtime) is request.app.state.runtimes[sandbox_id]
    assert rpc._context(request, runtime) is request.app.state.runtimes[sandbox_id]

    assert [r.message for r in caplog.records] == [expected]


def test_drift_warning_repeats_after_window_elapses(caplog, _isolated_throttle):
    sandbox_id = "sbx_drift_2"
    request = _request_with(sandbox_id, _DriftingCtx())
    runtime = _runtime(sandbox_id)
    caplog.set_level(logging.WARNING, logger="envd_service.rpc")
    ticks = _isolated_throttle
    expected = (
        "drift network update for sandbox sbx_drift_2 is not expressible "
        "on the live instance (egress model flip); keeping runtime policy"
    )

    rpc._context(request, runtime)
    ticks[0] = 30.0
    rpc._context(request, runtime)
    ticks[0] = 59.9
    rpc._context(request, runtime)
    assert [r.message for r in caplog.records] == [expected]

    ticks[0] = 60.0
    rpc._context(request, runtime)
    assert [r.message for r in caplog.records] == [expected, expected]

    ticks[0] = 90.0
    rpc._context(request, runtime)
    assert [r.message for r in caplog.records] == [expected, expected]
