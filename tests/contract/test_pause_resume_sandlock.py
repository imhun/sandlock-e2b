"""Pause/resume semantics on a real sandlock worker (M4 D5 contract).

D5 keeps the pause mechanism unchanged: ``ProcessManager.pause_all/resume_all``
raw-``SIGSTOP``/``SIGCONT`` each managed exec child's process group
(``envd_service/process/manager.py``), which works because every confined exec
child is its own group leader under sandlock fork F1.7 (verified directly
against a real sandlock child: Probe A in ``tmp/sdd/task-5-report.md``).

This file runs that verification through the SDK in the deployment shape where
pause actually reaches the worker: one ``RuntimeRegistry`` shared by the
control plane and the envd service (same wiring as ``tests/conftest.py``
``live_servers``), with ``E2B_EXECUTOR=sandlock`` semantics (pure sandlock, no
base image). Real multinode is not used here because remote control-to-worker
pause delivery is a pre-existing product gap (no agent route; registered as a
follow-up), independent of M4.

Skipped outside the Linux sandlock runner (macOS host runs cover the executor
mapping unit-level; the container runs this file with
``E2B_TEST_STRICT_SKIPS=1``).
"""

from __future__ import annotations

import queue
import threading

import pytest

from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings
from e2b import Sandbox
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.runtime.registry import RuntimeRegistry
from tests.conftest import TMP_ROOT, _ServerThread, _free_port, _fresh_dir
from tests.security.conftest import sandlock_ready

pytestmark = pytest.mark.skipif(
    not sandlock_ready(),
    reason=(
        "sandlock pause/resume contract tests need Linux + sandlock "
        "(run inside the Docker test runner)"
    ),
)

# The SDK ``CommandHandle.wait`` has no timeout, so completion is observed
# through a thread that pushes the outcome onto a queue. The window while the
# sandbox is paused is 0.5s: short enough to catch an un-frozen child that
# would have ended on its own, long enough not to trip on scheduling noise.
PAUSED_SILENCE_WINDOW_S = 0.5


@pytest.fixture()
def sandlock_combined_harness() -> dict[str, str]:
    """Control plane + envd service sharing one runtime registry.

    Mirrors ``tests.conftest.live_servers`` but with the real sandlock
    executor and pure sandlock shape (``base_image=None``: no image rootfs,
    no buildkit). Pause/resume reach ``ProcessManager.pause_all/resume_all``
    through the shared registry's state callbacks, so the SIGSTOP semantics
    under test are the ones the SDK actually drives.
    """
    workspace = _fresh_dir(TMP_ROOT / "pause-resume-sandlock")
    runtime_registry = RuntimeRegistry(workspace)
    control_port = _free_port()
    envd_port = _free_port()
    control_app = create_control_app(
        settings=ControlSettings(
            api_keys=("local-key",),
            control_plane_port=control_port,
            envd_port=envd_port,
            # Pure sandlock: keep the record image-less even if the runner
            # exports E2B_BASE_IMAGE.
            base_image=None,
            max_sandboxes=500,
            max_total_memory_mb=0,
            max_total_cpu_percent=0,
            max_total_disk_mb=0,
            max_total_processes=0,
            create_rate_limit_per_min=0,
        ),
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    envd_app = create_envd_app(
        settings=EnvdSettings(
            executor="sandlock",
            envd_port=envd_port,
        ),
        runtime_registry=runtime_registry,
        workspace_base=workspace,
    )
    control = _ServerThread(control_app, control_port)
    envd = _ServerThread(envd_app, envd_port)
    control.start()
    envd.start()
    try:
        yield {
            "api_url": f"http://127.0.0.1:{control_port}",
            "sandbox_url": f"http://127.0.0.1:{envd_port}",
        }
    finally:
        envd.stop()
        control.stop()


def test_pause_stops_exec_child_group_until_connect_resumes(
    sandlock_combined_harness,
) -> None:
    """A background command is frozen by pause() and completes only after
    Sandbox.connect() auto-resumes the sandbox."""
    harness = sandlock_combined_harness
    sandbox = Sandbox.create(
        api_url=harness["api_url"],
        sandbox_url=harness["sandbox_url"],
        api_key="local-key",
    )
    handle = None
    waiter = None
    try:
        handle = sandbox.commands.run("sleep 0.6; echo done", background=True)
        ended = queue.Queue(maxsize=1)

        def _wait_for_end() -> None:
            try:
                ended.put(handle.wait())
            except BaseException as exc:  # surface any wait failure exactly
                ended.put(exc)

        waiter = threading.Thread(target=_wait_for_end, daemon=True)
        waiter.start()

        assert sandbox.pause() is True
        # The child would have ended on its own by now; while paused it must
        # stay silent (no end event within the window).
        with pytest.raises(queue.Empty):
            ended.get(timeout=PAUSED_SILENCE_WINDOW_S)

        Sandbox.connect(
            sandbox.sandbox_id,
            api_url=harness["api_url"],
            sandbox_url=harness["sandbox_url"],
            api_key="local-key",
        )
        outcome = ended.get(timeout=15)
        if isinstance(outcome, BaseException):
            raise outcome
        assert outcome.stdout == "done\n"
        assert outcome.stderr == ""
        assert outcome.exit_code == 0
    finally:
        if handle is not None:
            handle.kill()
        if waiter is not None:
            waiter.join(timeout=5)
        sandbox.kill()
