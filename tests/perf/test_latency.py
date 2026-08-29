"""Latency / concurrency performance tests with profile recording."""

from __future__ import annotations

import asyncio
import cProfile
import pstats
import statistics
import time
from pathlib import Path

import pytest


@pytest.mark.perf
async def test_sandbox_create_p50(live_servers):
    from e2b import Sandbox

    samples = []
    sandboxes = []
    for _ in range(30):
        start = time.perf_counter()
        sandboxes.append(Sandbox.create())
        samples.append((time.perf_counter() - start) * 1000)
    for sb in sandboxes:
        sb.kill()
    p50 = statistics.median(samples)
    profile_dir = Path("tmp/perf")
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "create-latency.txt").write_text(
        f"p50={p50:.2f}ms min={min(samples):.2f}ms max={max(samples):.2f}ms n={len(samples)}\n"
    )
    assert p50 < 100, f"create P50 {p50:.2f}ms exceeds budget"


@pytest.mark.perf
def test_command_first_byte_p50(live_servers):
    from e2b import Sandbox

    sandbox = Sandbox.create()
    try:
        samples = []
        for _ in range(20):
            start = time.perf_counter()
            result = sandbox.commands.run("echo x")
            samples.append((time.perf_counter() - start) * 1000)
            assert result.exit_code == 0
        p50 = statistics.median(samples)
        Path("tmp/perf").mkdir(parents=True, exist_ok=True)
        Path("tmp/perf/command-latency.txt").write_text(
            f"p50={p50:.2f}ms n={len(samples)}\n"
        )
        assert p50 < 100, f"command P50 {p50:.2f}ms exceeds budget"
    finally:
        sandbox.kill()


@pytest.mark.perf
async def test_concurrent_sandboxes_no_deadlock(live_servers):
    from e2b import Sandbox

    def create_one():
        return Sandbox.create()

    loop = asyncio.get_running_loop()
    sandboxes = await asyncio.gather(
        *(loop.run_in_executor(None, create_one) for _ in range(50))
    )
    try:
        assert len(sandboxes) == 50
        assert all(s.is_running() for s in sandboxes)
    finally:
        for sb in sandboxes:
            sb.kill()
    Path("tmp/perf").mkdir(parents=True, exist_ok=True)
    Path("tmp/perf/concurrency.txt").write_text("50 concurrent sandboxes OK\n")


@pytest.mark.perf
def test_profile_recorded(live_servers):
    from e2b import Sandbox

    profiler = cProfile.Profile()
    profiler.enable()
    sandbox = Sandbox.create()
    sandbox.commands.run("echo profile")
    sandbox.kill()
    profiler.disable()
    profile_dir = Path("tmp/perf")
    profile_dir.mkdir(parents=True, exist_ok=True)
    stats = pstats.Stats(profiler)
    stats.dump_stats(str(profile_dir / "gateway.prof"))
    assert (profile_dir / "gateway.prof").is_file()

