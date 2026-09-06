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
    """A pty session on a real sandlock worker echoes input, survives a
    mid-session resize, and exits cleanly."""
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
            sandbox.pty.send_stdin(pty.pid, b"PS1=\necho pty-ok\n")
            sandbox.pty.resize(pty.pid, PtySize(rows=40, cols=120))
            sandbox.pty.send_stdin(pty.pid, b"exit\n")
            chunks = []
            result = pty.wait(on_pty=lambda data: chunks.append(data))
            assert result.exit_code == 0
            output = b"".join(chunks)
            # PS1= empties every prompt after the first line is parsed, but
            # the shell's first prompt (default `# ` root / `$ ` non-root) is
            # printed before it can parse PS1= and may land before or after
            # the echoed input chunk depending on scheduling. The transcript
            # therefore ends in one of the four exact tails (echoed input +
            # marker output + echoed exit), never a partial match.
            normalized = output.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            transcript_tails = (
                b"PS1=\necho pty-ok\n# pty-ok\nexit\n",
                b"PS1=\necho pty-ok\n$ pty-ok\nexit\n",
                b"# PS1=\necho pty-ok\npty-ok\nexit\n",
                b"$ PS1=\necho pty-ok\npty-ok\nexit\n",
            )
            assert normalized.endswith(transcript_tails)
        finally:
            pty.kill()
    finally:
        sandbox.kill()
