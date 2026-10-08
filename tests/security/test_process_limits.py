"""Process / memory / timeout limits."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.security.conftest import (
    require_mediation_capable,
    own_identity_sandbox,
    run_sh,
    sandbox_tmpdir,
)

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
    script = (
        "import os,sys\n"
        "pids=[]\n"
        "try:\n"
        "    for _ in range(200):\n"
        "        p=os.fork()\n"
        "        if p==0:\n"
        "            import time; time.sleep(30)\n"
        "            os._exit(0)\n"
        "        pids.append(p)\n"
        "except OSError:\n"
        "    sys.exit(7)\n"
        "import time; time.sleep(1)\n"
        "sys.exit(0)\n"
    )
    # The children must *stay alive*: `max_processes` is a whole-box ceiling
    # over live processes, so a burst of forks whose children exit before the
    # next one is registered never reaches it (measured 2026-09-25 in this very
    # shape: 200 exit-immediately forks pass under a ceiling of 8, 200 holding
    # children are refused). The old probe used the exit-immediately shape and
    # only ever "passed" because its hand-built executor could not create a
    # sandbox at all.
    # Through the deployment's shape (N15): the ceiling this exercises is
    # carried by the instance, and a hand-built in-process one is refused on a
    # root worker now that the pure shape is mediated (SL-1). The probe goes
    # from a file because it carries newlines a `sh -c` string would eat.
    executor, workspace = own_identity_sandbox(
        None, None, workspace=sandbox_tmpdir(), max_processes=8
    )
    try:
        require_mediation_capable(executor)
        (Path(workspace) / "fork_probe.py").write_text(script)
        code, out, err = asyncio.run(
            run_sh(executor, workspace, "/usr/local/bin/python3 /workspace/fork_probe.py")
        )
        # 7 is the probe's own "the fork was refused" exit -- pinned exactly, so
        # a sandbox that failed to start cannot satisfy this assertion.
        assert code == 7, f"exit={code} out={out!r} err={err!r}"
    finally:
        executor.close()
