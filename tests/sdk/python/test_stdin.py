"""stdin / close_stdin via the official e2b SDK."""

from __future__ import annotations

import pytest


def test_stdin_roundtrip(sandbox):
    proc = sandbox.commands.run("cat", stdin=True, background=True)
    proc.send_stdin("abc\n")
    proc.close_stdin()
    result = proc.wait()
    assert result.stdout == "abc\n"
    assert result.stderr == ""
    assert result.exit_code == 0


def test_stdin_multiple_chunks(sandbox):
    proc = sandbox.commands.run("cat", stdin=True, background=True)
    proc.send_stdin("one\n")
    proc.send_stdin("two\n")
    proc.close_stdin()
    result = proc.wait()
    assert result.stdout == "one\ntwo\n"
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_async_stdin(async_sandbox):
    proc = await async_sandbox.commands.run("cat", stdin=True, background=True)
    await proc.send_stdin("async-stdin\n")
    await proc.close_stdin()
    result = await proc.wait()
    assert result.stdout == "async-stdin\n"
    assert result.exit_code == 0

