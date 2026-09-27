#!/usr/bin/env python3
"""N37 probes: is the cut an *idle* one, and does output hold the stream open?

Each scenario is a fresh sandbox and one command. The SDK sends
``Keepalive-Ping-Interval: 50`` on ``process.Process/Start`` (see
``e2b/sandbox_sync/commands/command.py``), i.e. the cloud envd is expected to
emit an in-band ``keepalive`` event every 50 s; this worker's process stream
emits data/end only (``envd_service/rpc.py::_consume_stream``). So the
discriminating question is whether a *silent* command dies at ~60 s (a proxy
idle timeout) while one that keeps writing output survives.
"""

from __future__ import annotations

import argparse
import time
import traceback


def scenarios() -> list[tuple[str, str]]:
    silent_90 = (
        "python3 -c \"import time; time.sleep(90); print('done', flush=True)\""
    )
    chatty_95 = "python3 -c \"" + (
        "import time\n"
        "for i in range(19):\n"
        "    print(i, flush=True)\n"
        "    time.sleep(5)\n"
        "print('done', flush=True)\n"
    ) + "\""
    silent_50 = (
        "python3 -c \"import time; time.sleep(50); print('done', flush=True)\""
    )
    tree_4000_chatty = (
        "python3 -c \"\n"
        "import os, time\n"
        "root = '/home/user/probe'\n"
        "os.makedirs(root, exist_ok=True)\n"
        "for i in range(4000):\n"
        "    open(os.path.join(root, f'f{i:05d}.bin'), 'wb').write(b'x' * 512)\n"
        "    if i % 100 == 0:\n"
        "        print(i, flush=True)\n"
        "print('created', len(os.listdir(root)), flush=True)\n"
        "\""
    )
    return [
        ("silent-90s", silent_90),
        ("chatty-95s", chatty_95),
        ("silent-50s", silent_50),
        ("tree-4000-chatty", tree_4000_chatty),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", action="append", default=None)
    args = parser.parse_args()

    from e2b import Sandbox

    for label, cmd in scenarios():
        if args.only and label not in args.only:
            continue
        sandbox = None
        try:
            sandbox = Sandbox.create(timeout=1800)
            started = time.monotonic()
            try:
                result = sandbox.commands.run(cmd, timeout=900)
                took = time.monotonic() - started
                print(f"{label}: OK after {took:.1f}s exit={result.exit_code} "
                      f"stdout={result.stdout.strip().splitlines()[-1:]!r}", flush=True)
            except Exception as exc:  # noqa: BLE001 - the failure is the datum
                took = time.monotonic() - started
                print(f"{label}: FAILED after {took:.1f}s {type(exc).__name__}: "
                      f"{str(exc)[:200]}", flush=True)
                traceback.print_exc(limit=1)
        finally:
            if sandbox is not None:
                try:
                    sandbox.kill()
                except Exception:  # noqa: BLE001
                    pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
