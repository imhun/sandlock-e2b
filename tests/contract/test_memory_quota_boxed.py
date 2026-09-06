"""FUP-E3 contract: one sandbox instance's boxed 1 GiB quota denies overcommit.

The original plan paired the over-budget command with a live MCP gateway
(gateway holds 450M, command 450M denied, command 50M accepted). That pairing
is not expressible on the current sandlock fork wheel: a multithreaded
gateway process poisons later exec creations (argv-safety freeze EPERM, exit
127; gateway-shape blocker evidence: ``tmp/perf/task8-fup3-evidence-100-450-50.txt``),
and the gateway's own ledger left <200M headroom inside the old 512M box, so
a 450M MCP server child could not start there. That ledger-headroom half of
the blocker is now closed E2B-side (FUP #3) by raising the per-sandbox default
memory ceiling from 512 MiB to 1 GiB; fork F11 remains open. The D8-allowed
pure-sandlock sibling variant below formalizes the exact same-instance
accounting through the SDK:

* one background command child holds 800 MiB (touched pages) on the sandbox's
  single long-lived instance;
* a second concurrent 400 MiB command must be denied with the exact SDK
  signature recorded in ``tmp/perf/task8-fup3-sdk-signature-variants.txt``
  (host-present evidence; ``CommandExitException`` exit_code 137, empty
  stdout, error None, stderr one of the recorded exact set
  ``{"", "Killed\\n"}``);
* with the same 800 MiB holder, a concurrent 50 MiB command succeeds exactly
  (exit 0, stdout ``got 50\n``) and the holder later finishes cleanly.

Skipped outside the Linux sandlock runner (macOS host runs cover the executor
mapping unit-level instead; run inside the Docker test runner with
``E2B_BASE_IMAGE=`` for the pure-sandlock shape and
``E2B_MAX_CONCURRENT_COMMANDS_PER_SANDBOX=2`` so two commands share the slot).
"""

from __future__ import annotations

import time

import httpx
import pytest
import queue
import threading

from e2b import Sandbox
from e2b.sandbox.commands.command_handle import CommandExitException
from tests.security.conftest import sandlock_ready

pytestmark = pytest.mark.skipif(
    not sandlock_ready(),
    reason=(
        "sandlock boxed memory quota contract tests need Linux + sandlock "
        "(run inside the Docker test runner)"
    ),
)

#: Default per-sandbox memory ceiling asserted through the public record.
DEFAULT_MEMORY_MB = 1024

HOLDER_MB = 800
OVER_BUDGET_SIBLING_MB = 400
CONTROL_SIBLING_MB = 50

#: Exact SDK-visible denial signature recorded by the Step-1 probe
#: (tmp/perf/task8-fup3-sdk-signature-variants.txt): the sandlock supervisor SIGKILLs
#: the over-budget python task and its bash wrapper reports 128+9. The
#: wrapper's stderr carries an optional shell notice ("Killed") depending on
#: how the kill lands, so the exact observed set is asserted, never a
#: substring.
DENIED_EXIT_CODE = 137
DENIED_STDERR_SET = ("", "Killed\n")


def alloc_script(mb: int, hold: float) -> str:
    """Python script: allocate ``mb`` MiB, touch every page, print, and hold."""
    parts = [
        "import time",
        f"buf=bytearray({mb}*1024*1024)",
        "for i in range(0,len(buf),4096): buf[i]=1",
        f"print('got {mb}',flush=True)",
    ]
    parts.append(f"time.sleep({hold})")
    return "\n".join(parts)


def py_cmd(script: str) -> str:
    """SDK command line running ``script`` under python3."""
    return f"/usr/local/bin/python3 -c \"{script}\""


def sandbox_opts(harness) -> dict[str, str]:
    return {
        "api_url": harness["api_url"],
        "sandbox_url": harness["sandbox_url"],
        "api_key": "local-key",
    }


def start_holder_waiter(handle):
    """Consume the background holder's stream on a waiter thread.

    Returns ``(ready, done, thread)``: ``ready`` carries the holder MiB
    exactly when the accumulated stdout equals ``got 800\n``; ``done``
    carries the holder's terminal ``("ok", CommandResult)`` / ``("err",
    CommandExitException)``. Only this thread may consume the handle.
    """
    ready: queue.Queue[str] = queue.Queue(maxsize=1)
    done: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

    def _wait_holder() -> None:
        accumulated: list[str] = []

        def _on_stdout(chunk: str) -> None:
            accumulated.append(chunk)
            if "".join(accumulated) == f"got {HOLDER_MB}\n":
                try:
                    ready.put_nowait(str(HOLDER_MB))
                except queue.Full:
                    pass

        try:
            result = handle.wait(on_stdout=_on_stdout)
            done.put(("ok", result))
        except CommandExitException as exc:
            done.put(("err", exc))

    thread = threading.Thread(target=_wait_holder, daemon=True)
    thread.start()
    return ready, done, thread


def wait_for_holder_allocation(ready, done, deadline_s: float = 30.0) -> None:
    """Wait until the holder's stdout is exactly ``got 800\n`` (loud on early
    holder exit or timeout)."""
    deadline = time.time() + deadline_s
    holder_ended: tuple[str, object] | None = None
    while time.time() < deadline:
        try:
            if ready.get_nowait() == str(HOLDER_MB):
                return
        except queue.Empty:
            pass
        try:
            holder_ended = done.get_nowait()
            break
        except queue.Empty:
            pass
        time.sleep(0.1)
    if holder_ended is not None:
        kind, payload = holder_ended
        if kind == "ok":
            result = payload  # type: ignore[assignment]
            raise AssertionError(
                f"{HOLDER_MB} MiB holder ended before allocating: "
                f"exit_code={result.exit_code} stdout={result.stdout!r} "
                f"stderr={result.stderr!r}"
            )
        exc = payload  # type: ignore[assignment]
        raise AssertionError(
            f"{HOLDER_MB} MiB holder failed before allocating: "
            f"exit_code={exc.exit_code} stdout={exc.stdout!r} "
            f"stderr={exc.stderr!r}"
        )
    raise AssertionError(
        f"{HOLDER_MB} MiB holder never printed its allocation marker; the "
        "over-budget assertion would be vacuous"
    )


def test_boxed_memory_quota_denies_sibling_overcommit(
    multinode_two_workers,
) -> None:
    """Two concurrent SDK commands on one instance share the 1 GiB box quota."""
    harness = multinode_two_workers
    sandbox = Sandbox.create(**sandbox_opts(harness))
    holder = None
    holder_thread = None
    holder_ready = None
    holder_done = None
    try:
        # The sandbox is created with the default per-sandbox memory: the
        # public record must say exactly 1024 MiB.
        detail = httpx.get(
            f"{harness['api_url'].rstrip('/')}/sandboxes/{sandbox.sandbox_id}",
            headers={"X-API-Key": "local-key"},
            timeout=10,
        )
        assert detail.status_code == 200
        assert detail.json()["memoryMB"] == DEFAULT_MEMORY_MB

        # Holder: 800 MiB background command, alive until its sleep ends.
        holder = sandbox.commands.run(
            py_cmd(alloc_script(HOLDER_MB, hold=30.0)),
            background=True,
            timeout=120,
        )
        holder_ready, holder_done, holder_thread = start_holder_waiter(holder)
        wait_for_holder_allocation(holder_ready, holder_done)

        # Over-budget sibling: 800 + 400 = 1200 > 1024 MiB (plus interpreter
        # overhead), so the new task is killed by the boxed memory supervisor;
        # the SDK sees the exact recorded rejection (bash reports the
        # SIGKILLed python as 128+9).
        with pytest.raises(CommandExitException) as excinfo:
            sandbox.commands.run(
                py_cmd(alloc_script(OVER_BUDGET_SIBLING_MB, hold=2.0)),
                timeout=60,
            )
        assert excinfo.value.exit_code == DENIED_EXIT_CODE
        assert excinfo.value.stdout == ""
        assert excinfo.value.stderr in DENIED_STDERR_SET
        assert excinfo.value.error is None

        # Control sibling: 800 + 50 plus interpreter overhead stays below
        # 1024 MiB while the holder still owns its allocation, so the command
        # must succeed exactly.
        control = sandbox.commands.run(
            py_cmd(alloc_script(CONTROL_SIBLING_MB, hold=2.0)),
            timeout=60,
        )
        assert control.exit_code == 0
        assert control.stdout == "got 50\n"
        assert control.stderr == ""

        # The holder survived both siblings and exits cleanly on its own.
        if holder_done is not None:
            kind, payload = holder_done.get(timeout=45)
            if kind == "err":
                exc = payload  # type: ignore[assignment]
                raise AssertionError(
                    f"{HOLDER_MB} MiB holder ended early: "
                    f"exit_code={exc.exit_code} stdout={exc.stdout!r} "
                    f"stderr={exc.stderr!r}"
                ) from exc
            result_holder = payload  # type: ignore[assignment]
            assert result_holder.exit_code == 0
            assert result_holder.stdout == f"got {HOLDER_MB}\n"
            assert result_holder.stderr == ""
    finally:
        if holder_thread is not None:
            holder_thread.join(timeout=1)
        if holder is not None:
            try:
                holder.kill()
            except Exception:
                pass
        try:
            sandbox.kill()
        except Exception:
            pass
