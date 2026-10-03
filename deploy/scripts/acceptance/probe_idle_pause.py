#!/usr/bin/env python3
"""Acceptance for the idle->pause sweep: does an untouched sandbox park itself?

Creates one sandbox with a timeout long enough to outlive the sweep, sends it
no traffic at all, and polls ``GET /sandboxes/{id}`` until the control plane
reports ``state: paused``. Then it resumes the sandbox through the SDK's own
surface (``POST /sandboxes/{id}/connect``) and checks it comes back
``running``. Everything it created is deleted in a ``finally`` -- a probe that
leaves sandboxes behind is how the fleet got the eight stale records the TTL
sweep could not explain (N61).

What this probe deliberately does **not** cover: the reverse direction ("a
stream that is held open keeps the sandbox running"). That one needs a
client that holds an exec stream for minutes, so it is a manual step with the
SDK::

    sbx = Sandbox.create(timeout=900)
    sbx.commands.run("sleep 400", timeout=400)   # held open, no output, no CPU

and the answer is that ``GET /sandboxes/{id}`` stays ``running`` past the
threshold. The worker-side keep-alive that makes that true is pinned by
``tests/unit/test_stream_activity_keepalive.py``.

``--wait-s`` must exceed the deployment's ``E2B_IDLE_PAUSE_AFTER_S`` by at
least one sweep interval (15 s) plus the activity-persist lag (30 s); the
default is 420 s for the shipped 300 s threshold. A probe that gives up early
reports the states it saw, by name, and exits 1.

Needs ``E2B_API_KEY`` in the environment and refuses by name, exit code 2,
when it is missing -- the same convention as the other acceptance probes here.

Importing this module has no side effects (``main`` is only called under
``__main__``), so the helpers below can be exercised by a unit test.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any, Callable, Iterable, Sequence

import httpx

DEFAULT_BASE_URL = "http://172.18.78.49:3000"
DEFAULT_TEMPLATE = "base"
DEFAULT_SANDBOX_TIMEOUT_S = 900
DEFAULT_WAIT_S = 420.0
DEFAULT_POLL_S = 5.0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", default=os.getenv("E2B_API_URL", DEFAULT_BASE_URL))
    parser.add_argument("--template", default=DEFAULT_TEMPLATE)
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_SANDBOX_TIMEOUT_S,
        help="sandbox timeout in seconds; must outlive the whole probe",
    )
    parser.add_argument(
        "--wait-s",
        type=float,
        default=DEFAULT_WAIT_S,
        help="how long to wait for the pause (must exceed E2B_IDLE_PAUSE_AFTER_S)",
    )
    parser.add_argument("--poll-s", type=float, default=DEFAULT_POLL_S)
    return parser.parse_args(argv)


def state_of(payload: Any) -> str:
    """The ``state`` field of a sandbox payload, or ``"unknown"``."""
    if not isinstance(payload, dict):
        return "unknown"
    return str(payload.get("state", "unknown"))


def wait_for_state(
    fetch: Callable[[], Any],
    target: str,
    *,
    wait_s: float,
    poll_s: float,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, list[tuple[float, str]]]:
    """Poll ``fetch`` until it reports ``target`` or ``wait_s`` elapses.

    Returns ``(reached, samples)`` where each sample is ``(elapsed_s, state)``.
    Returning the samples instead of raising is what makes a timeout usable:
    the caller prints the states it actually saw.
    """
    started = clock()
    samples: list[tuple[float, str]] = []
    while True:
        elapsed = clock() - started
        state = state_of(fetch())
        samples.append((elapsed, state))
        if state == target:
            return True, samples
        if elapsed >= wait_s:
            return False, samples
        sleep(poll_s)


def _headers(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key}


def _fetch_state(client: httpx.Client, base_url: str, api_key: str, sandbox_id: str) -> str:
    response = client.get(f"{base_url}/sandboxes/{sandbox_id}", headers=_headers(api_key))
    response.raise_for_status()
    return state_of(response.json())


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    api_key = (os.getenv("E2B_API_KEY") or "").strip()
    if not api_key:
        print(
            "probe_idle_pause: refusing to run -- E2B_API_KEY is not set "
            "(it is the only credential this probe uses)",
            file=sys.stderr,
        )
        return 2

    sandbox_id: str | None = None
    with httpx.Client(timeout=30) as client:
        try:
            created = client.post(
                f"{args.base_url}/sandboxes",
                headers=_headers(api_key),
                json={"templateID": args.template, "timeout": args.timeout},
            )
            created.raise_for_status()
            sandbox_id = str(created.json()["sandboxID"])
            print(
                f"probe_idle_pause: created {sandbox_id} "
                f"(timeout={args.timeout}s, template={args.template})"
            )

            def fetch() -> str:
                assert sandbox_id is not None
                return _fetch_state(client, args.base_url, api_key, sandbox_id)

            paused, samples = wait_for_state(
                fetch, "paused", wait_s=args.wait_s, poll_s=args.poll_s
            )
            for elapsed, state in samples:
                print(f"  t+{elapsed:6.1f}s state={state}")
            if not paused:
                print(
                    "probe_idle_pause: FAIL -- the sandbox never reported "
                    f"'paused' within {args.wait_s:.0f}s; is "
                    "E2B_IDLE_PAUSE_AFTER_S set in this deployment, and is it "
                    "smaller than --wait-s?",
                    file=sys.stderr,
                )
                return 1

            resumed = client.post(
                f"{args.base_url}/sandboxes/{sandbox_id}/connect",
                headers=_headers(api_key),
                json={},
            )
            resumed.raise_for_status()
            state = state_of(resumed.json())
            print(f"probe_idle_pause: connect -> state={state}")
            if state != "running":
                print(
                    "probe_idle_pause: FAIL -- connect did not bring the sandbox "
                    f"back to running (got {state!r}); the node it is pinned to "
                    "may have no room to re-admit it",
                    file=sys.stderr,
                )
                return 1

            print(
                "probe_idle_pause: OK -- idle sandbox parked and resumed "
                f"(paused at t+{samples[-1][0]:.1f}s)"
            )
            return 0
        finally:
            if sandbox_id is not None:
                try:
                    client.delete(
                        f"{args.base_url}/sandboxes/{sandbox_id}",
                        headers=_headers(api_key),
                    )
                    print(f"probe_idle_pause: deleted {sandbox_id}")
                except httpx.HTTPError as exc:  # pragma: no cover - cleanup
                    print(
                        f"probe_idle_pause: WARNING could not delete {sandbox_id}: {exc}",
                        file=sys.stderr,
                    )


if __name__ == "__main__":  # pragma: no cover - the probe runs on the cluster
    raise SystemExit(main())
