"""N28: the workspace writer runs its work *inside* the sandbox.

Two layers are pinned here, and the split is the point:

* a recording executor shows the *shape* of every write -- one ``/bin/sh -c``
  helper with the workspace as its cwd, the caller's path as an argv operand
  (never interpolated into the script), and no place in the command log;
* the real ``LocalExecutor`` runs the same helpers for real, so the snippets
  themselves (``mkdir -p``, ``mv``, ``rm -rf``, ``cat > tmp && mv``) are
  covered rather than assumed.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from envd_service.executors.base import ExecConfig, Executor, RunningProcess
from envd_service.executors.local import LocalExecutor
from envd_service.filesystem.ops import FilesystemOps
from envd_service.filesystem.writer import ARGV0, SHELL, SandboxWriter
from envd_service.process.manager import ProcessManager
from gateway_common.errors import ConnectError
from gateway_common.upload import UploadTooLargeError


class _RecordingProcess(RunningProcess):
    def __init__(self, pid: int, *, exit_code: int = 0, stderr: bytes = b"") -> None:
        self._pid = pid
        self._exit_code = exit_code
        self._stderr = stderr
        self.stdin = bytearray()
        self.stdin_closed = False

    @property
    def pid(self) -> int:
        return self._pid

    async def output(self) -> AsyncIterator[tuple[str, bytes]]:
        if self._stderr:
            yield ("stderr", self._stderr)

    async def feed_stdin(self, data: bytes) -> None:
        self.stdin.extend(data)

    def close_stdin(self) -> None:
        self.stdin_closed = True

    async def exit_code(self) -> int:
        return self._exit_code


class _RecordingExecutor(Executor):
    """Records every exec; optionally fails it with a chosen exit code."""

    def __init__(self, *, exit_code: int = 0, stderr: bytes = b"") -> None:
        self.configs: list[ExecConfig] = []
        self.processes: list[_RecordingProcess] = []
        self._exit_code = exit_code
        self._stderr = stderr

    async def start(self, config: ExecConfig) -> RunningProcess:
        self.configs.append(config)
        proc = _RecordingProcess(
            1000 + len(self.processes),
            exit_code=self._exit_code,
            stderr=self._stderr,
        )
        self.processes.append(proc)
        return proc


class _Context:
    """The slice of ``SandboxRuntimeContext`` the writer touches."""

    def __init__(self, root: Path, processes: ProcessManager) -> None:
        self.record = SimpleNamespace(
            sandbox_id="sbx_writer",
            workspace_dir=str(root),
            env_vars={"PATH": "/usr/bin:/bin"},
        )
        self.files = FilesystemOps(root)
        self.processes = processes


def _context(root: Path, executor: Executor | None = None, **manager_kwargs):
    manager = ProcessManager(
        executor or LocalExecutor(),
        sandbox_id="sbx_writer",
        **manager_kwargs,
    )
    return _Context(root, manager)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    path = tmp_path / "sbx_writer"
    path.mkdir()
    return path


def _chunks(*parts: bytes):
    async def gen():
        for part in parts:
            yield part

    return gen()


# -- the shape of a write ---------------------------------------------------


async def test_make_dir_calls_the_shell_inside_the_workspace(root):
    executor = _RecordingExecutor()
    writer = SandboxWriter(_context(root, executor))
    await writer.make_dir("a/b/c")

    assert len(executor.configs) == 1
    config = executor.configs[0]
    assert config.cmd == [
        SHELL,
        "-c",
        'set -e\nmkdir -p -- "$1"\n',
        ARGV0,
        "a/b/c",
    ]
    # The workspace itself is the helper's cwd: the sandbox sees that root at
    # ``/home/user``, which is what makes a root-relative path an ordinary
    # relative path to the sandbox.
    assert config.cwd == str(root)
    # No stdin to a helper that carries no payload.
    assert config.stdin_enabled is False


async def test_move_passes_both_ends_as_operands(root):
    executor = _RecordingExecutor()
    (root / "a.txt").write_text("x")
    writer = SandboxWriter(_context(root, executor))
    await writer.move("a.txt", "dir/b.txt")

    assert executor.configs[0].cmd == [
        SHELL,
        "-c",
        (
            "set -e\n"
            'mkdir -p -- "$(dirname -- "$2")"\n'
            'mv -- "$1" "$2"\n'
        ),
        ARGV0,
        "a.txt",
        "dir/b.txt",
    ]


async def test_remove_refuses_the_workspace_root(root):
    writer = SandboxWriter(_context(root, _RecordingExecutor()))
    with pytest.raises(ConnectError) as exc:
        await writer.remove("")
    assert exc.value.code == "invalid_argument"
    assert exc.value.message == "the sandbox root cannot be removed"
    assert root.is_dir()


async def test_a_path_with_a_quote_stays_one_operand(root):
    """Nothing is interpolated into the script: the path is argv, full stop."""
    executor = _RecordingExecutor()
    writer = SandboxWriter(_context(root, executor))
    await writer.make_dir('weird"; rm -rf /  #')

    config = executor.configs[0]
    assert config.cmd[2] == 'set -e\nmkdir -p -- "$1"\n'
    assert config.cmd[4] == 'weird"; rm -rf /  #'


# -- the helpers themselves (real shell) ------------------------------------


async def test_make_dir_real_shell_creates_the_nested_tree(root):
    writer = SandboxWriter(_context(root))
    await writer.make_dir("a/b/c")
    assert (root / "a" / "b" / "c").is_dir()


async def test_make_dir_real_shell_refuses_an_existing_path(root):
    writer = SandboxWriter(_context(root))
    (root / "a").mkdir()
    with pytest.raises(ConnectError) as exc:
        await writer.make_dir("a")
    assert exc.value.code == "already_exists"
    assert exc.value.message == "Path a already exists"


async def test_move_real_shell_creates_the_destination_parent(root):
    writer = SandboxWriter(_context(root))
    (root / "a.txt").write_text("x")
    await writer.move("a.txt", "dir/b.txt")
    assert (root / "dir" / "b.txt").read_text() == "x"
    assert not (root / "a.txt").exists()


async def test_remove_real_shell_takes_a_whole_tree(root):
    writer = SandboxWriter(_context(root))
    (root / "dir" / "sub").mkdir(parents=True)
    (root / "dir" / "f.txt").write_text("x")
    await writer.remove("dir")
    assert not (root / "dir").exists()


async def test_write_stream_publishes_the_body_and_leaves_no_temp(root):
    writer = SandboxWriter(_context(root))
    target = await writer.write_stream(
        "sub/big.bin", _chunks(b"a" * 100, b"b" * 100), limit_bytes=None
    )
    assert target == root / "sub" / "big.bin"
    assert target.read_bytes() == b"a" * 100 + b"b" * 100
    assert [p.name for p in target.parent.iterdir()] == ["big.bin"]


async def test_write_stream_over_the_limit_publishes_nothing(root):
    writer = SandboxWriter(_context(root))
    with pytest.raises(UploadTooLargeError):
        await writer.write_stream(
            "big.bin", _chunks(b"x" * 100, b"x" * 100), limit_bytes=150
        )
    assert list(root.iterdir()) == []


async def test_write_stream_replaces_an_existing_file_in_place(root):
    """The rename is what makes the replacement atomic (E4.2)."""
    writer = SandboxWriter(_context(root))
    (root / "f.bin").write_bytes(b"old")
    await writer.write_stream("f.bin", _chunks(b"new"), limit_bytes=None)
    assert (root / "f.bin").read_bytes() == b"new"


async def test_write_stream_rejects_a_path_that_escapes_the_root(root):
    writer = SandboxWriter(_context(root))
    with pytest.raises(ConnectError) as exc:
        await writer.write_stream("../outside", _chunks(b"x"), limit_bytes=None)
    assert exc.value.code == "invalid_argument"
    assert exc.value.message == "path escapes the sandbox root"


async def test_a_failing_helper_reports_the_sandbox_stderr(root):
    executor = _RecordingExecutor(exit_code=1, stderr=b"mv: cannot stat 'a'\n")
    writer = SandboxWriter(_context(root, executor))
    with pytest.raises(ConnectError) as exc:
        await writer.make_dir("a")
    assert exc.value.code == "internal"
    assert exc.value.message == (
        "MakeDir failed inside the sandbox: mv: cannot stat 'a'"
    )


async def test_a_helper_killed_by_a_pause_reports_the_pause(tmp_path):
    """N28/B: the write that raced the pause gets the same refusal as the gate.

    A helper that dies by signal while its sandbox is not ``running`` is not a
    worker fault to report as a 500 -- the caller can resume and retry, and the
    message says so (with the platform's reason, when it has one).
    """
    executor = _RecordingExecutor(exit_code=-signal.SIGKILL)
    context = _context(root := tmp_path / "sbx_writer", executor)
    context.record.state = "paused"
    context.record.pause_reason = (
        "its workspace grew past its budget (1340 MiB used of 1024 MiB)"
    )
    writer = SandboxWriter(context)

    with pytest.raises(ConnectError) as exc:
        await writer.write_stream("f.bin", _chunks(b"x"), limit_bytes=None)

    assert exc.value.code == "failed_precondition"
    assert exc.value.http_status == 409
    assert exc.value.message == (
        "Sandbox is paused: its workspace grew past its budget "
        "(1340 MiB used of 1024 MiB); the Upload was interrupted by the pause "
        "rather than frozen (resume it and retry)"
    )


async def test_a_helper_that_fails_on_its_own_is_still_a_worker_fault(tmp_path):
    executor = _RecordingExecutor(exit_code=1, stderr=b"mv: cannot stat 'a'\n")
    context = _context(root := tmp_path / "sbx_writer", executor)
    writer = SandboxWriter(context)

    with pytest.raises(ConnectError) as exc:
        await writer.make_dir("a")

    assert exc.value.code == "internal"
    assert exc.value.http_status == 500


# -- internal is internal --------------------------------------------------


async def test_the_helper_is_not_written_to_the_command_log(root):
    logged: list[tuple] = []
    executor = _RecordingExecutor()
    manager = ProcessManager(
        executor,
        sandbox_id="sbx_writer",
        on_command_log=lambda proc, event, payload: logged.append((event, payload)),
    )
    writer = SandboxWriter(_Context(root, manager))
    await writer.make_dir("a")
    assert logged == []


async def test_the_helper_does_not_queue_behind_a_user_command(root):
    """A 1-concurrent-command sandbox still answers a write (N28).

    ``E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX`` defaults to 1 and a user
    command holds the gate for its whole lifetime, so a write that took the
    gate would sit in the queue until the command ended (30s) and then fail
    with 429. The writer is the platform acting as the sandbox: it is not a
    user command and must not contend with one.
    """
    manager = ProcessManager(
        LocalExecutor(),
        sandbox_id="sbx_writer",
        max_concurrent_commands=1,
        max_queued_commands=0,
    )
    writer = SandboxWriter(_Context(root, manager))
    held = await manager.start(
        cmd=[SHELL, "-c", "sleep 30"],
        env={"PATH": "/usr/bin:/bin"},
        cwd=str(root),
        stdin_enabled=False,
    )
    try:
        await asyncio.wait_for(writer.make_dir("a"), timeout=10)
        assert (root / "a").is_dir()
    finally:
        manager.send_signal(held.pid, 9)


# -- metadata ---------------------------------------------------------------


async def test_persist_metadata_is_best_effort_when_python3_is_missing(root):
    """No ``python3`` in the image must not fail the upload that just landed."""
    executor = _RecordingExecutor(exit_code=127, stderr=b"sh: python3: not found\n")
    writer = SandboxWriter(_context(root, executor))
    (root / "f.bin").write_bytes(b"x")
    assert await writer.persist_metadata("f.bin", {"k": "v"}) is False


async def test_persist_metadata_is_a_no_op_without_metadata(root):
    executor = _RecordingExecutor()
    writer = SandboxWriter(_context(root, executor))
    assert await writer.persist_metadata("f.bin", {}) is True
    assert executor.configs == []


async def test_persist_metadata_passes_its_operands_in_the_script_s_order(root):
    """The one helper that takes *code* as an operand: pin the argv order.

    ``$1`` is the JSON, ``$2`` the path and ``$3`` the program, because the
    program is the only operand that may contain characters the shell would
    otherwise act on -- it is read through ``"$3"``, never interpolated.
    """
    executor = _RecordingExecutor()
    writer = SandboxWriter(_context(root, executor))
    (root / "f.bin").write_bytes(b"x")

    assert await writer.persist_metadata("f.bin", {"owner": "alice"}) is True

    cmd = executor.configs[0].cmd
    assert cmd[:4] == [SHELL, "-c", 'exec python3 -c "$3" "$1" "$2"\n', ARGV0]
    assert cmd[4] == '{"owner":"alice"}'
    assert cmd[5] == "f.bin"
    assert cmd[6].startswith("import os,sys;")


@pytest.mark.skipif(
    not hasattr(os, "setxattr"),
    reason="os.setxattr is Linux-only, and so is the worker (E5.1)",
)
async def test_persist_metadata_reaches_the_file_through_the_sandbox(root):
    writer = SandboxWriter(_context(root))
    (root / "f.bin").write_bytes(b"x")

    assert await writer.persist_metadata("f.bin", {"owner": "alice"}) is True

    assert os.getxattr(root / "f.bin", "user.e2b.owner") == b"alice"


# -- the helper runs where the tree lives ----------------------------------


async def test_the_writer_only_ever_names_paths_under_its_root(tmp_path):
    """A file that exists beside the root is ``not_found``, not a sibling hit."""
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    root.mkdir()
    sibling.mkdir()
    (sibling / "keep.txt").write_text("keep")
    writer = SandboxWriter(_context(root))
    with pytest.raises(ConnectError) as exc:
        await writer.remove("keep.txt")
    assert exc.value.code == "not_found"
    assert (sibling / "keep.txt").read_text() == "keep"
