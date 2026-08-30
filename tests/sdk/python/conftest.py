"""Fixtures for official e2b SDK tests (sync + async)."""

from __future__ import annotations

import pytest

from e2b import AsyncSandbox, Sandbox


@pytest.fixture()
def sandbox(live_servers):
    sb = Sandbox.create()
    yield sb
    try:
        sb.kill()
    except Exception:
        pass


@pytest.fixture()
async def async_sandbox(live_servers):
    sb = await AsyncSandbox.create()
    yield sb
    try:
        await sb.kill()
    except Exception:
        pass
