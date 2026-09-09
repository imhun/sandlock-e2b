"""PTY through the sandlock executor's held instance (M4 D3 contract).

Runs against a real multinode harness with sandlock workers (Docker test
runner): an SDK pty session is created on the sandbox, fed ``echo pty-ok``,
resized mid-session, then exited, and the merged pty output must carry the
echoed marker with a clean exit -- proving the per-exec native
``ExecStdio.PTY`` path (host-side master + ``ExecProcess.resize``) replaces
the removed in-sandbox bridge. The same flow only ever ran against the local
executor before (tests/sdk/python/test_pty.py); this file is the true
sandlock slot.

Skipped outside the Linux sandlock runner (macOS host runs cover the
executor mapping unit-level instead; Step 7 runs in the container with
``E2B_TEST_STRICT_SKIPS=1``).
"""

from __future__ import annotations

import pytest

from e2b.sandbox.commands.command_handle import PtySize
from tests.security.conftest import sandlock_ready

pytestmark = pytest.mark.skipif(
    not sandlock_ready(),
    reason=(
        "sandlock PTY contract tests need Linux + sandlock "
        "(run inside the Docker test runner)"
    ),
)


def test_pty_echo_resize_exit_on_sandlock_instance(multinode_two_workers) -> None:
    """A pty session on a real sandlock worker echoes input, a mid-session
    resize reaches the child, and it exits cleanly."""
    from e2b import Sandbox

    harness = multinode_two_workers
    sandbox = Sandbox.create(
        api_url=harness["api_url"],
        sandbox_url=harness["sandbox_url"],
        api_key="local-key",
    )
    try:
        pty = sandbox.pty.create(PtySize(rows=24, cols=80))
        try:
            assert pty.pid > 0
            sandbox.pty.send_stdin(pty.pid, b"PS1=\n")
            # Resize *before* asking the child about the window, so the answer
            # is deterministic: 40x120 can only come from a resize that
            # arrived. (Route B owns the master in the worker process, so this
            # is the pty path's resize contract.)
            sandbox.pty.resize(pty.pid, PtySize(rows=40, cols=120))
            sandbox.pty.send_stdin(pty.pid, b"stty size; echo pty-ok\n")
            sandbox.pty.send_stdin(pty.pid, b"exit\n")
            chunks = []
            result = pty.wait(on_pty=lambda data: chunks.append(data))
            assert result.exit_code == 0
            output = b"".join(chunks)
            normalized = output.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            # The transcript merges four independent writers on the master --
            # the input echo, the shell's "no controlling terminal" banner,
            # its first prompt, and the command output -- and their relative
            # order is scheduling, not semantics (the banner/prompt position
            # differs between the in-process instance and a supervise slot).
            # Assert every piece exactly once instead of pinning one merge
            # order; a lost echo, a lost output line or a doubled prompt all
            # still fail here.
            banner = b"/bin/sh: 0: can't access tty; job control turned off\n"
            assert normalized.count(b"PS1=\n") == 1, normalized
            assert normalized.count(b"stty size; echo pty-ok\n") == 1, normalized
            assert normalized.count(b"exit\n") == 1, normalized
            assert normalized.count(banner) == 1, normalized
            assert normalized.count(b"pty-ok\n") == 2, normalized  # echo + output
            assert normalized.count(b"40 120\n") == 1, normalized
            assert (
                normalized.count(b"# ") + normalized.count(b"$ ")
            ) == 1, (
                "exactly one shell prompt is expected before PS1= is parsed; "
                f"got {normalized!r}"
            )
            # The window size the worker set is what the child reported, and
            # the marker came from the command, not the echo.
            assert normalized.index(b"stty size; echo pty-ok\n") < normalized.rindex(
                b"pty-ok\n"
            )
        finally:
            pty.kill()
    finally:
        sandbox.kill()
