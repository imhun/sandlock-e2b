"""Command execution via the official e2b SDK."""

from __future__ import annotations

import time

import pytest

from e2b.exceptions import TimeoutException
from e2b.sandbox.commands.command_handle import CommandExitException


def test_command_result_is_exact(sandbox):
    result = sandbox.commands.run("echo hello")
    assert result.stdout == "hello\n"
    assert result.stderr == ""
    assert result.exit_code == 0


def test_command_stderr_and_exit_code(sandbox):
    with pytest.raises(CommandExitException) as exc:
        sandbox.commands.run("echo err >&2; exit 3")
    assert exc.value.stdout == ""
    assert exc.value.stderr == "err\n"
    assert exc.value.exit_code == 3


def test_command_exit_code_is_exact(sandbox):
    with pytest.raises(CommandExitException) as exc:
        sandbox.commands.run("exit 7")
    assert exc.value.exit_code == 7
    assert exc.value.stdout == ""
    assert exc.value.stderr == ""


def test_background_command_wait(sandbox):
    proc = sandbox.commands.run("echo background", background=True)
    assert proc.pid > 0
    result = proc.wait()
    assert result.stdout == "background\n"
    assert result.stderr == ""
    assert result.exit_code == 0


def test_commands_list_and_kill(sandbox):
    proc = sandbox.commands.run("sleep 30", background=True)
    pid = proc.pid
    procs = sandbox.commands.list()
    assert any(p.pid == pid for p in procs)
    assert sandbox.commands.kill(pid) is True
    assert sandbox.commands.kill(pid) is False
    deadline = time.time() + 5
    while time.time() < deadline:
        if all(p.pid != pid for p in sandbox.commands.list()):
            break
        time.sleep(0.1)
    assert all(p.pid != pid for p in sandbox.commands.list())


def test_command_connect(sandbox):
    proc = sandbox.commands.run("cat", stdin=True, background=True)
    pid = proc.pid
    connected = sandbox.commands.connect(pid)
    connected.send_stdin("via-connect\n")
    connected.close_stdin()
    result = connected.wait()
    assert result.stdout == "via-connect\n"
    assert result.exit_code == 0


def test_command_timeout_raises_and_process_cleaned(sandbox):
    with pytest.raises(TimeoutException):
        sandbox.commands.run("sleep 5", timeout=1)
    deadline = time.time() + 10
    while time.time() < deadline:
        if sandbox.commands.list() == []:
            break
        time.sleep(0.2)
    assert sandbox.commands.list() == []


def test_command_cwd(sandbox):
    result = sandbox.commands.run("pwd", cwd="workspace")
    assert result.exit_code == 0
    assert result.stdout.strip().endswith("/workspace")


@pytest.mark.asyncio
async def test_async_command_result(async_sandbox):
    result = await async_sandbox.commands.run("echo async-hello")
    assert result.stdout == "async-hello\n"
    assert result.stderr == ""
    assert result.exit_code == 0


@pytest.mark.asyncio
async def test_async_exit_code(async_sandbox):
    from e2b.sandbox.commands.command_handle import CommandExitException

    with pytest.raises(CommandExitException) as exc:
        await async_sandbox.commands.run("exit 7")
    assert exc.value.exit_code == 7
