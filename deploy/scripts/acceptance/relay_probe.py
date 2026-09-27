#!/usr/bin/env python3
"""N37 end-to-end: a 60 s idle cut in front of the local stack.

The fleet's edge cuts a response body that has been silent for 60.0 s
(measured; the SDK reports it as ``unexpected EOF during chunk size line``).
This script puts the same cut in front of the *local* stack -- the in-process
control plane + worker + gateway the contract tests use -- so the fix can be
verified end to end without touching the shared cluster:

  client (e2b SDK) -> idle relay (60 s) -> control plane + worker

Scenarios (each a fresh sandbox, one command):

* ``silent-90s``: 90 s of silence. Dies at ~60 s without the fix.
* ``tree-4000``: one command writing 4000 files, paced so it runs ~90 s --
  the failing shape from N37 (a silent 4000-file write on the fleet's NFS),
  and the acceptance criterion (>= 3 consecutive passes).

Run it twice: once with the keepalive removed (RED), once as shipped (GREEN).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from pathlib import Path

REPO = Path("/workspace")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

IDLE_CUT_S = 60.0


def tree_command(count: int, root: str, sleep_ms: float) -> str:
    return (
        "python3 -c \"\n"
        "import os, time\n"
        f"root = {root!r}\n"
        "os.makedirs(root, exist_ok=True)\n"
        f"for i in range({count}):\n"
        "    open(os.path.join(root, f'f{i:05d}.bin'), 'wb').write(b'x' * 512)\n"
        f"    time.sleep({sleep_ms / 1000.0})\n"
        "print('created', len(os.listdir(root)), flush=True)\n"
        "\""
    )


class IdleRelay:
    """TCP relay that drops a connection idle in *both* directions.

    It runs on its own thread with its own event loop: the e2b SDK is
    synchronous, so driving it from inside an event loop would starve the
    relay and measure the probe's own deadlock instead of the cut.
    """

    def __init__(self, upstream_port: int, idle_s: float) -> None:
        self._upstream_port = upstream_port
        self._idle_s = idle_s
        self._server: asyncio.AbstractServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.port = 0
        self.cuts = 0

    def start(self) -> int:
        threading.Thread(target=self._run, daemon=True).start()
        deadline = time.monotonic() + 10
        while not self.port:
            if time.monotonic() > deadline:
                raise RuntimeError("the relay did not start")
            time.sleep(0.01)
        return self.port

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self._loop.run_forever()

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    def stop(self) -> None:
        if self._loop is None or self._server is None:
            return
        close = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
        try:
            close.result(timeout=5)
        except Exception:  # noqa: BLE001 - teardown must not mask a verdict
            pass

    async def _shutdown(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        up_reader, up_writer = await asyncio.open_connection(
            "127.0.0.1", self._upstream_port
        )
        state = {"deadline": time.monotonic() + self._idle_s}
        cut = await asyncio.gather(
            self._pump(reader, up_writer, state),
            self._pump(up_reader, writer, state),
        )
        if any(cut):
            self.cuts += 1
        for w in (writer, up_writer):
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass

    async def _pump(self, reader, writer, state) -> bool:
        """Copy one direction; True when the copy ended on the idle timer."""
        while True:
            remaining = state["deadline"] - time.monotonic()
            if remaining <= 0:
                print(f"    relay: idle {self._idle_s:.0f}s -- cutting", flush=True)
                return True
            try:
                chunk = await asyncio.wait_for(reader.read(65536), timeout=remaining)
            except asyncio.TimeoutError:
                print(f"    relay: idle {self._idle_s:.0f}s -- cutting", flush=True)
                return True
            except (ConnectionResetError, OSError):
                return False
            if not chunk:
                return False
            state["deadline"] = time.monotonic() + self._idle_s
            try:
                writer.write(chunk)
                await writer.drain()
            except (ConnectionResetError, OSError):
                return False


def scenarios(files: int, sleep_ms: float) -> list[tuple[str, str, float]]:
    return [
        (
            "silent-90s",
            "python3 -c \"import time; time.sleep(90); print('done', flush=True)\"",
            IDLE_CUT_S + 90.0,
        ),
        (
            f"tree-{files}",
            tree_command(files, "/home/user/n37", sleep_ms),
            IDLE_CUT_S + 90.0,
        ),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=4000)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--sleep-ms", type=float, default=22.0)
    parser.add_argument(
        "--idle-s",
        type=float,
        default=IDLE_CUT_S,
        help="idle cut the relay imposes (the fleet edge measured 60)",
    )
    parser.add_argument("--only", action="append", default=None)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    from tests.conftest import _start_multinode

    scratch = Path(os.environ.get("E2B_TEST_TMP_ROOT", "/var/lib/e2b-test-runtime"))
    harness = _start_multinode(
        (scratch / "n37" / "relay-stack"), 1, warm_base_image=True
    )
    os.environ["E2B_API_KEY"] = "local-key"

    from e2b import Sandbox

    api_port = int(harness["api_url"].rsplit(":", 1)[1])
    sandbox_port = int(harness["sandbox_url"].rsplit(":", 1)[1])

    def run_scenario(label: str, cmd: str, repeat: int) -> bool:
        """One fresh sandbox through its *own* pair of edges.

        Fresh relays per scenario on purpose: the SDK's transport pools
        connections by URL, and a relay that drops an idle *pooled*
        connection (which this one does, being only an idle timer) would then
        hand the next RPC a dead socket -- the "server closed first" flake
        ``gateway_common/keepalive.py`` documents, and an artifact of the
        probe rather than the failure under test. A new URL per scenario
        keeps every measurement on a connection this scenario created.
        """
        api_relay = IdleRelay(api_port, args.idle_s)
        sandbox_relay = IdleRelay(sandbox_port, args.idle_s)
        api_relay.start()
        sandbox_relay.start()
        os.environ["E2B_API_URL"] = f"http://127.0.0.1:{api_relay.port}"
        os.environ["E2B_SANDBOX_URL"] = f"http://127.0.0.1:{sandbox_relay.port}"
        sandbox = None
        started = time.monotonic()
        try:
            sandbox = Sandbox.create(timeout=1800)
            result = sandbox.commands.run(cmd, timeout=900)
            took = time.monotonic() - started
            print(
                f"{label} #{repeat + 1}: OK after {took:.1f}s "
                f"exit={result.exit_code} out={result.stdout.strip()!r}",
                flush=True,
            )
            return True
        except Exception as exc:  # noqa: BLE001 - the failure is the datum
            print(
                f"{label} #{repeat + 1}: FAILED after "
                f"{time.monotonic() - started:.1f}s "
                f"{type(exc).__name__}: {str(exc)[:240]}",
                flush=True,
            )
            traceback.print_exc(limit=1)
            return False
        finally:
            if sandbox is not None:
                try:
                    sandbox.kill()
                except Exception:  # noqa: BLE001
                    pass
            cuts = api_relay.cuts + sandbox_relay.cuts
            print(f"{label} #{repeat + 1}: relay cuts = {cuts}", flush=True)
            api_relay.stop()
            sandbox_relay.stop()

    failures = 0
    try:
        for label, cmd, _budget in scenarios(args.files, args.sleep_ms):
            if args.only and label not in args.only:
                continue
            repeats = args.runs if label.startswith("tree-") else 1
            for repeat in range(repeats):
                if not run_scenario(label, cmd, repeat):
                    failures += 1
    finally:
        harness["_stop"]()
    print(f"RELAY PROBE DONE: failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
