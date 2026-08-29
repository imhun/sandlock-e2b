"""PTY tests via the official e2b SDK (local executor and Sandlock bridge)."""

from __future__ import annotations

import time

import pytest

from e2b.sandbox.commands.command_handle import PtySize


def test_pty_create_send_resize_kill(sandbox):
    pty = sandbox.pty.create(PtySize(rows=24, cols=80))
    try:
        assert pty.pid > 0
        sandbox.pty.send_stdin(pty.pid, b"echo pty-ok\n")
        sandbox.pty.resize(pty.pid, PtySize(rows=40, cols=120))
        sandbox.pty.send_stdin(pty.pid, b"exit\n")
        chunks = []
        result = pty.wait(on_pty=lambda data: chunks.append(data))
        assert result.exit_code == 0
        output = b"".join(chunks)
        assert b"pty-ok" in output
    finally:
        pty.kill()
