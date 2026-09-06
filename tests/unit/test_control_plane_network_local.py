"""Control-plane ``local://`` network update atomicity (M4 D4 review).

A live local runtime context must validate AND apply in one atomic
``ctx.update_network`` call before the registry record is saved: a conflict
surfaces as HTTP 409 (a closed-instance failure as the appropriate error) and
never persists; a success persists the record only after the worker-side
apply succeeded.
"""

from __future__ import annotations

import httpx

from gateway_common.network import NetworkUpdateConflictError


class _FakeLocalCtx:
    """Recording ctx stand-in: accepts, rejects with a conflict, or closes."""

    def __init__(self, *, reject_message: str | None = None, closed: bool = False):
        self.reject_message = reject_message
        self.closed = closed
        self.calls: list[dict | None] = []

    def update_network(self, network):
        self.calls.append(dict(network) if network else None)
        if self.closed:
            raise RuntimeError("sandlock instance is closed")
        if self.reject_message is not None:
            raise NetworkUpdateConflictError(self.reject_message)


def _client(control, *, raise_app_exceptions: bool = True):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=control, raise_app_exceptions=raise_app_exceptions
        ),
        base_url="http://test",
    )


async def _create(client) -> str:
    resp = await client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={
            "templateID": "base",
            "timeout": 300,
            "metadata": {"user": "alice"},
            "envVars": {},
            "secure": True,
            "allow_internet_access": False,
        },
    )
    assert resp.status_code == 201
    return resp.json()["sandboxID"]


async def _detail(client, sandbox_id) -> dict:
    resp = await client.get(
        f"/sandboxes/{sandbox_id}", headers={"X-API-Key": "local-key"}
    )
    assert resp.status_code == 200
    return resp.json()


def _record_network(control, sandbox_id):
    return control.state.registry.get(sandbox_id).network


async def test_local_apply_conflict_returns_409_and_never_persists(make_apps):
    """A live local ctx rejecting the update yields 409 with the registry
    record byte-identical -- no 204, no persisted record."""
    control, _envd = make_apps()
    async with _client(control) as client:
        sandbox_id = await _create(client)
        before = (await _detail(client, sandbox_id))["network"]
        before_record = _record_network(control, sandbox_id)
        ctx = _FakeLocalCtx(
            reject_message=(
                "network egress model cannot change on a launched sandbox "
                "(allowOut -> denyOut)"
            )
        )
        control.state.runtimes = {sandbox_id: ctx}

        resp = await client.put(
            f"/sandboxes/{sandbox_id}/network",
            headers={"X-API-Key": "local-key"},
            json={"denyOut": ["10.0.0.0/8"]},
        )

        assert resp.status_code == 409
        assert resp.json() == {
            "code": 409,
            "message": (
                "network egress model cannot change on a launched sandbox "
                "(allowOut -> denyOut)"
            ),
        }
        assert ctx.calls == [{"denyOut": ["10.0.0.0/8"]}]
        after = (await _detail(client, sandbox_id))["network"]
        assert after == before
        assert _record_network(control, sandbox_id) == before_record


async def test_local_apply_closed_instance_returns_error_and_never_persists(
    make_apps,
):
    """A closed-instance apply failure must not become a silent 204 with a
    persisted record either."""
    control, _envd = make_apps()
    async with _client(control, raise_app_exceptions=False) as client:
        sandbox_id = await _create(client)
        before = (await _detail(client, sandbox_id))["network"]
        before_record = _record_network(control, sandbox_id)
        control.state.runtimes = {sandbox_id: _FakeLocalCtx(closed=True)}

        resp = await client.put(
            f"/sandboxes/{sandbox_id}/network",
            headers={"X-API-Key": "local-key"},
            json={"allowOut": ["8.8.8.8"]},
        )

        assert resp.status_code == 500
        after = (await _detail(client, sandbox_id))["network"]
        assert after == before
        assert _record_network(control, sandbox_id) == before_record


async def test_local_apply_success_persists_after_worker_apply(make_apps):
    """On success the ctx apply runs first (one atomic call) and only then is
    the registry record saved."""
    control, _envd = make_apps()
    async with _client(control) as client:
        sandbox_id = await _create(client)
        ctx = _FakeLocalCtx()
        control.state.runtimes = {sandbox_id: ctx}

        resp = await client.put(
            f"/sandboxes/{sandbox_id}/network",
            headers={"X-API-Key": "local-key"},
            json={"allowOut": ["8.8.8.8"]},
        )

        assert resp.status_code == 204
        assert ctx.calls == [{"allowOut": ["8.8.8.8"]}]
        detail = await _detail(client, sandbox_id)
        assert detail["network"] == {"allowOut": ["8.8.8.8"]}
        assert control.state.registry.get(sandbox_id).network == {
            "allowOut": ["8.8.8.8"]
        }
