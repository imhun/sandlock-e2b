"""E4.1 contract: ``cat /dev/zero`` style output cannot grow the capture cache.

The worker's per-stream capture is capped at ``E2B_COMMAND_CAPTURE_LIMIT_MB``
with an exact truncation marker in the replay buffer. This drives the real
local executor (a real ``head`` subprocess reading /dev/zero) through the
same settings wiring the worker uses, then asserts the captured replay
boundary byte-for-byte.
"""

from __future__ import annotations

import asyncio

import httpx

from envd_service.config import Settings as EnvdSettings
from envd_service.process.manager import TRUNCATED_MARK


async def _create_sandbox(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


async def test_dev_zero_capture_capped_with_exact_marker(make_apps):
    control, envd = make_apps(
        envd_settings=EnvdSettings(executor="local", command_capture_limit_mb=1)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        sandbox = await _create_sandbox(client)

    runtime = envd.state.runtime_registry.get(sandbox["sandboxID"])
    assert runtime is not None
    ctx = envd.state.context_factory(runtime)
    envd.state.runtimes[sandbox["sandboxID"]] = ctx
    proc = await ctx.processes.start(
        cmd=["/bin/sh", "-c", "head -c 2000000 /dev/zero"],
        env={},
        cwd=runtime.workspace_dir,
        stdin_enabled=False,
    )
    queue = proc.subscribe(replay=False)
    assert (
        await asyncio.wait_for(ctx.processes.wait_ended(proc, queue), timeout=30)
    )[0] == "end"

    limit = 1024 * 1024
    buf = proc.captured["stdout"]
    assert len(buf) == limit
    assert "stdout" in proc.captured_truncated
    assert buf.count(TRUNCATED_MARK) == 1
    assert bytes(buf[: -len(TRUNCATED_MARK)]) == b"\0" * (
        limit - len(TRUNCATED_MARK)
    )
    assert bytes(buf[-len(TRUNCATED_MARK):]) == TRUNCATED_MARK

    # The replay a late Connect subscriber would receive is exactly the
    # capped buffer: head content up to the cap, then the marker.
    replay_queue = proc.subscribe(replay=True)
    replayed = b""
    while not replay_queue.empty():
        item = replay_queue.get_nowait()
        if item[0] == "data":
            replayed += item[2]
    assert replayed == bytes(buf)
