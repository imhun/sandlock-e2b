"""Process / memory / timeout limits."""

from __future__ import annotations

import asyncio

import pytest

from tests.security.conftest import sandbox_tmpdir

from envd_service.executors.local import LocalExecutor
from envd_service.process.manager import ProcessManager


@pytest.mark.asyncio
async def test_command_timeout_kills_and_cleans(workspace):
    manager = ProcessManager(LocalExecutor(), max_command_timeout=1)
    proc = await manager.start(
        cmd=["sleep", "30"],
        env={},
        cwd=str(workspace),
        stdin_enabled=False,
    )
    queue = proc.subscribe(replay=False)
    end = await asyncio.wait_for(manager.wait_ended(proc, queue), timeout=5)
    assert end[0] == "end"
    assert end[1] == -9
    assert end[2] == "killed"
    assert manager.get_or_none(proc.pid) is None


@pytest.mark.usefixtures("require_sandlock")
def test_max_processes_limits_forks():
    from envd_service.executors.base import ExecConfig
    from envd_service.executors.sandlock import SandlockExecutor

    import tempfile

    ws = str(sandbox_tmpdir())
    executor = SandlockExecutor(
        workspace_dir=ws,
        base_image=None,
        image_rootfs=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=8,
        max_open_files=4096,
        allow_internet_access=False,
        enable_network=False,
    )
    script = (
        "import os,sys\n"
        "pids=[]\n"
        "try:\n"
        "    for _ in range(200):\n"
        "        p=os.fork()\n"
        "        if p==0: os._exit(0)\n"
        "        pids.append(p)\n"
        "except OSError:\n"
        "    sys.exit(7)\n"
        "for p in pids:\n"
        "    os.waitpid(p,0)\n"
        "sys.exit(0)\n"
    )
    result = executor._build_sandbox(
        ExecConfig(
            cmd=["/usr/local/bin/python3", "-c", script],
            env={},
            cwd=ws,
            stdin_enabled=False,
        )
    ).run(["/usr/local/bin/python3", "-c", script])
    assert result.exit_code != 0
