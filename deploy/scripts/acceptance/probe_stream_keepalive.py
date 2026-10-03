#!/usr/bin/env python3
"""Acceptance for E9.1 keep-alive: a held-open stream is not idleness.

The idle->pause sweep reads ``lastActiveAt``. A silent long command --
``sleep 400``, a slow ``make``, an exec waiting on a remote API -- produces no
chunks and no measurable CPU, so without the worker-side keep-alive
(``envd_service/connect/router.py``, one re-stamp per coalescing window while
a stream is open) the stamp would freeze at the moment the command started and
``E2B_IDLE_PAUSE_AFTER_S`` would freeze the command underneath the client.

This probe opens one sandbox, holds a silent command stream open, and samples
the list view every 20 s. Both must hold at the end:

* the sandbox never left ``running`` -- the sweep would have paused it if it
  only looked at "no client requests"; and
* ``lastActiveAt`` kept moving *during* the silent hold (>= 3 distinct values),
  which is the mechanism rather than a coincidence.

Run it against the same deployment as ``probe_idle_pause.py`` (the other half
of the acceptance: idle -> paused -> resumed). It needs the sandbox SDK
(``from e2b import Sandbox``) and ``E2B_API_KEY``; like the other probes here it
refuses by name, exit code 2, when the credential is missing.

``--hold-s`` must stay well above the keep-alive cadence and below the
deployment's idle threshold: the default 150 s proves ~15 re-stamps and says
nothing about the threshold, so "still running" can only be the keep-alive.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Sequence

import httpx

DEFAULT_BASE_URL = "http://172.18.78.49:3000"
DEFAULT_HOLD_S = 150
DEFAULT_SANDBOX_TIMEOUT_S = 900
DEFAULT_SAMPLE_EVERY_S = 20.0
#: Distinct ``lastActiveAt`` values a silent hold of ``--hold-s`` must produce.
MIN_DISTINCT_STAMPS = 3


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.getenv("E2B_API_URL", DEFAULT_BASE_URL))
    parser.add_argument("--hold-s", type=int, default=DEFAULT_HOLD_S)
    parser.add_argument("--timeout", type=int, default=DEFAULT_SANDBOX_TIMEOUT_S)
    parser.add_argument("--sample-every-s", type=float, default=DEFAULT_SAMPLE_EVERY_S)
    return parser.parse_args(argv)


def read_state(client: httpx.Client, base_url: str, api_key: str, sandbox_id: str) -> tuple[str, str]:
    """``(state, lastActiveAt)`` from the list view (the item payload has neither)."""
    response = client.get(f"{base_url}/sandboxes", headers={"X-API-Key": api_key})
    response.raise_for_status()
    for entry in response.json():
        if entry.get("sandboxID") == sandbox_id:
            return str(entry.get("state", "unknown")), str(entry.get("lastActiveAt", ""))
    return "gone", ""


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = (os.getenv("E2B_API_KEY") or "").strip()
    if not api_key:
        print(
            "probe_stream_keepalive: refusing to run -- E2B_API_KEY is not set "
            "(it is the only credential this probe uses)",
            file=sys.stderr,
        )
        return 2
    try:
        from e2b import Sandbox
    except ImportError as exc:  # pragma: no cover - the harness has it
        print(
            f"probe_stream_keepalive: refusing to run -- the sandbox SDK is not "
            f"importable ({exc}); this probe needs `from e2b import Sandbox`",
            file=sys.stderr,
        )
        return 2

    client = httpx.Client(timeout=30)
    sandbox = Sandbox.create(
        api_url=args.base_url, sandbox_url=args.base_url, api_key=api_key,
        timeout=args.timeout,
    )
    sandbox_id = sandbox.sandbox_id
    print(
        f"probe_stream_keepalive: created {sandbox_id} "
        f"(timeout={args.timeout}s, hold={args.hold_s}s)"
    )

    outcome: dict[str, str] = {}

    def hold() -> None:
        try:
            # No `background=True` on purpose: this call *is* the held stream.
            sandbox.commands.run(f"sleep {args.hold_s}", timeout=args.hold_s + 120)
            outcome["result"] = "command finished"
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            outcome["result"] = f"command raised: {exc!r}"

    thread = threading.Thread(target=hold, daemon=True)
    started = time.monotonic()
    thread.start()
    samples: list[tuple[float, str, str]] = []
    try:
        while time.monotonic() - started < args.hold_s:
            time.sleep(args.sample_every_s)
            state, last_active = read_state(
                client, args.base_url, api_key, sandbox_id
            )
            elapsed = time.monotonic() - started
            samples.append((elapsed, state, last_active))
            print(
                f"  t+{elapsed:6.1f}s state={state} lastActiveAt={last_active}",
                flush=True,
            )
        thread.join(timeout=120)
        print(f"probe_stream_keepalive: stream {outcome.get('result', 'still open')}")

        states = {state for _, state, _ in samples}
        stamps = {stamp for _, _, stamp in samples if stamp}
        if states != {"running"}:
            print(
                "probe_stream_keepalive: FAIL -- the state left `running` while a "
                f"stream was held open: {sorted(states)}",
                file=sys.stderr,
            )
            return 1
        if len(stamps) < MIN_DISTINCT_STAMPS:
            print(
                "probe_stream_keepalive: FAIL -- lastActiveAt did not keep moving "
                f"during a silent hold ({len(stamps)} distinct values in "
                f"{len(samples)} samples); the stream keep-alive is not working",
                file=sys.stderr,
            )
            return 1
        print(
            f"probe_stream_keepalive: OK -- state stayed running for {args.hold_s}s; "
            f"lastActiveAt moved {len(stamps)} times during the silent hold"
        )
        return 0
    finally:
        sandbox.kill()
        client.close()
        print(f"probe_stream_keepalive: deleted {sandbox_id}")


if __name__ == "__main__":  # pragma: no cover - the probe runs on the cluster
    raise SystemExit(main())
