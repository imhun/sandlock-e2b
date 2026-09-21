#!/usr/bin/env python3
"""Warm a worker's base image through that worker's own agent endpoint.

Why this exists (measured 2026-09-20, `docs/k8s-deployment.md` §22.5.10): a
worker rollout can interrupt the base-image warm-up, and the image cache then
holds only the ``…sha256_….lock`` with no extracted rootfs next to it.  The
next ``Sandbox.create()`` on that node answers **428 warm_required** -- which
the e2b SDK neither sends ``X-Sandbox-Id`` for nor understands -- so a cold
node turns into a failed smoke for a reason that has nothing to do with the
sandbox itself.  Warming at the end of a rollout removes the window: the
first create on a freshly rolled node hits the fast path.

Two details the endpoint's shape forces:

* **POST warms, GET only peeks.**  ``GET /agent/images/{ref}/warm`` returns
  ``{"cached": bool, "digest": …}`` without extracting anything; only
  ``POST`` resolves the rootfs.  Both are idempotent, and both take
  ``X-Internal-Key``.
* **It runs inside the worker container**, not on the deploy host.  The agent
  listens on the container's own ``0.0.0.0:49983`` (``envd_service/__main__``),
  and the compose stack publishes no host port for it, so the caller hands this
  file to ``python3 -`` in the container (``kubectl exec -i``) or pipes it
  through ``docker compose exec -T``.

Output is two JSON lines (``peek`` then ``warm``) plus a final one-line
verdict, so a rollout log can be read without decoding JSON by hand.  Exit
status is 0 only when the node ends up warm (or reports that its executor
needs no images at all -- the local fallback shape), 1 otherwise: a rollout
that cannot warm its nodes must say so instead of leaving the next create to
discover it.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

#: The agent's own port; the same default the compose stack and the k8s
#: manifests use (`envd_service/config.py`'s ``envd_port``).
DEFAULT_URL = "http://127.0.0.1:49983"

#: Extracting a cold python-slim rootfs took 18.1 s on the cluster, and a
#: cold node may also be pulling the image; the endpoint itself answers only
#: when the rootfs is on disk, so the client waits rather than guessing.
DEFAULT_TIMEOUT_S = 900.0


def _request(url: str, *, key: str, method: str, timeout: float) -> dict:
    """One warm call. Returns the decoded body (``{}`` for an empty 200)."""
    req = urllib.request.Request(
        url, method=method, headers={"X-Internal-Key": key}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else {}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Warm (or peek at) a worker's base image via its agent."
    )
    parser.add_argument("--image", required=True, help="base-image reference")
    parser.add_argument("--key", required=True, help="X-Internal-Key value")
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"agent base URL (default {DEFAULT_URL})",
    )
    parser.add_argument(
        "--peek-only",
        action="store_true",
        help="only run the GET query; never POST",
    )
    parser.add_argument(
        "--timeout-s", type=float, default=DEFAULT_TIMEOUT_S, help="per-request timeout"
    )
    args = parser.parse_args(argv)

    # Exactly the encoding the control plane uses (`control_plane/api/
    # sandboxes.py::_warm_node`): a digest-pinned reference keeps its `@`, its
    # `:` and its `/`, everything else is percent-encoded.
    quoted = urllib.parse.quote(args.image, safe="/:")
    url = f"{args.url.rstrip('/')}/agent/images/{quoted}/warm"

    try:
        peek = _request(url, key=args.key, method="GET", timeout=args.timeout_s)
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        print(f"peek failed: {exc}", file=sys.stderr)
        return 1
    print("peek " + json.dumps(peek, sort_keys=True))

    # A worker whose executor never resolves images answers `required: false`
    # for both verbs: there is nothing to warm and nothing to wait for.
    if peek.get("required") is False:
        print(f"RESULT image={args.image} cached=true required=false warmed=no-op")
        return 0

    state = peek
    warmed = "skipped"
    if not peek.get("cached"):
        if args.peek_only:
            warmed = "no"
        else:
            try:
                state = _request(
                    url, key=args.key, method="POST", timeout=args.timeout_s
                )
                warmed = "yes"
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                print(f"warm failed: {exc}", file=sys.stderr)
                return 1
            print("warm " + json.dumps(state, sort_keys=True))

        if state.get("required") is False:
            print(f"RESULT image={args.image} cached=true required=false warmed=no-op")
            return 0

    cached = bool(state.get("cached"))
    print(
        f"RESULT image={args.image} cached={'true' if cached else 'false'} "
        f"required=true warmed={warmed}"
    )
    return 0 if cached else 1


if __name__ == "__main__":
    sys.exit(main())
