"""G1a contract: pause/resume delivery reaches a real remote worker.

The combined shape (``test_pause_resume_sandlock.py``) freezes exec children
through one runtime registry shared by the control plane and the envd
service. This file locks the separated (multinode) shape: the control plane
pushes pause/resume to the hosting worker agent (``/agent/sandboxes/{id}/
pause|resume``), and a real sandlock worker must freeze a background command
until the SDK's ``Sandbox.connect`` auto-resume thaws it.

Shape notes (identical to the combined contract):

* one background ``sleep 1.2; echo done`` command;
* ``sandbox.pause()`` then a 2.0s silence window in which the child must
  stay frozen (had pause not reached the worker it would have ended);
* ``Sandbox.connect`` auto-resume, then the exact ``done``/exit-0 outcome.

Pause freezes the currently running command groups; it does not gate future
execs (parity with the combined deployment).

Skipped outside the Linux sandlock runner (macOS host runs cover the delivery
mapping unit-level; the container runs this file with
``E2B_TEST_STRICT_SKIPS=1`` and an empty ``E2B_BASE_IMAGE`` for the pure
sandlock shape).
"""

from __future__ import annotations

import queue
import threading

import pytest

from e2b import Sandbox
from tests.security.conftest import sandlock_ready

pytestmark = pytest.mark.skipif(
    not sandlock_ready(),
    reason=(
        "sandlock pause/resume multinode contract tests need Linux + "
        "sandlock (run inside the Docker test runner)"
    ),
)

#: Same silence window as the combined contract: a sleep-1.2 child would have
#: ended within 2.0s if pause never reached the worker.
PAUSED_SILENCE_WINDOW_S = 2.0


def test_pause_delivery_freezes_remote_child_until_connect_resumes(
    multinode_two_workers,
) -> None:
    """A background command on a remote sandlock worker is frozen by
    pause() and completes only after Sandbox.connect() auto-resumes it."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(
        api_url=harness["api_url"],
        sandbox_url=harness["sandbox_url"],
        api_key="local-key",
    )
    handle = None
    waiter = None
    try:
        handle = sandbox.commands.run("sleep 1.2; echo done", background=True)
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

