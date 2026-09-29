#!/usr/bin/env python3
"""A recording HTTP proxy for the CP→agent hop (acceptance instrument, C3 判据 16).

Judgment 16 asks for evidence that the control plane's CP→agent concurrency
pool does **not** serialise slot grants -- and that squeezing that pool to one
*does*. The control plane dials the agent at ``E2B_C3_AGENT_URL``; this process
takes that address for the duration of the acceptance run and forwards every
instruction to the real agent unchanged (same method, path, body, token), while
writing one JSON line per request to a log:

    {"op": "grant-slot", "start": 1.7e9, "end": 1.7e9+0.02, "status": 200}

``start``/``end`` are ``time.time()`` on the instrument, so the concurrency of
the hop is *observable*: with the CP's pool at 64, three concurrent creates put
three overlapping ``grant-slot`` requests in flight; with the pool at 1 the
second request can only start after the first has finished, and the log shows
zero overlap. That property -- overlap vs no overlap -- is binary and needs no
timing threshold, which is why this is a better witness than end-to-end
latencies alone (it also survives a fast hop: an absent overlap is a fact, not a
noise level).

``HOP_DELAY_S`` optionally sleeps before forwarding, which only widens the
window in which an overlap *could* be seen; it is used when the unhurried hop is
too short for the arrival spread of the N creates to overlap even without a
pool. It never changes what any component does.

The proxy is deliberately outside the production code path: nothing here is
imported by the agent or the control plane, and the agent's own identity stays
in step with the URL the control plane dials (``E2B_C3_AGENT_NODE_ID`` must
equal the proxy's service name -- see ``deploy/compose/.env.example``).

Usage (inside the compose network):

    HOP_UPSTREAM_HOST=c3-agent HOP_UPSTREAM_PORT=49985 \\
        HOP_LOG=/log/hop.jsonl python3 c3_compose_hop_proxy.py
"""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPSTREAM_HOST = os.environ.get("HOP_UPSTREAM_HOST", "c3-agent")
UPSTREAM_PORT = int(os.environ.get("HOP_UPSTREAM_PORT", "49985"))
LISTEN_PORT = int(os.environ.get("HOP_LISTEN_PORT", str(UPSTREAM_PORT)))
DELAY_S = float(os.environ.get("HOP_DELAY_S", "0") or "0")
LOG_PATH = os.environ.get("HOP_LOG", "/log/hop.jsonl")
#: An optional file the run rewrites to change the delay without restarting the
#: instrument: the acceptance keeps one rig for every arm (right, baseline,
#: counter) so the arms differ only in the thing under test.
DELAY_FILE = os.environ.get("HOP_DELAY_FILE", "/log/delay")

#: One lock per append: several request threads write the same log file and a
#: torn line would be an unreadable witness.
_LOG_LOCK = threading.Lock()


def _record(entry: dict[str, object]) -> None:
    with _LOG_LOCK:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _delay_s() -> float:
    """Per-request delay: the file when it exists, else ``HOP_DELAY_S``."""
    try:
        with open(DELAY_FILE, encoding="utf-8") as handle:
            return float(handle.read().strip() or "0")
    except (OSError, ValueError):
        return DELAY_S


class HopProxy(BaseHTTPRequestHandler):
    #: ``HTTP/1.1`` keeps the client's keep-alive contract; the control plane
    #: opens one client per instruction either way.
    protocol_version = "HTTP/1.1"
    #: The instrument's own stderr must not fill the container log with one
    #: line per request; the JSONL file is the record.
    def log_message(self, *args: object) -> None:  # noqa: D102 - http.server hook
        return

    def do_POST(self) -> None:  # noqa: N802 - http.server hook
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        token = self.headers.get("X-Internal-Key", "")
        start = time.time()
        delay = _delay_s()
        if delay > 0:
            time.sleep(delay)
        try:
            connection = http.client.HTTPConnection(
                UPSTREAM_HOST, UPSTREAM_PORT, timeout=600
            )
            connection.request(
                "POST",
                self.path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body)),
                    "X-Internal-Key": token,
                },
            )
            response = connection.getresponse()
            payload = response.read()
            status = response.status
            connection.close()
        except OSError as exc:  # pragma: no cover - the upstream is up in a run
            end = time.time()
            _record(
                {
                    "op": self.path.rsplit("/", 1)[-1],
                    "path": self.path,
                    "start": start,
                    "end": end,
                    "status": 0,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        end = time.time()
        _record(
            {
                "op": self.path.rsplit("/", 1)[-1],
                "path": self.path,
                "start": start,
                "end": end,
                "status": status,
            }
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> int:
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), HopProxy)
    server.daemon_threads = True
    print(
        f"c3 hop proxy: 0.0.0.0:{LISTEN_PORT} -> "
        f"{UPSTREAM_HOST}:{UPSTREAM_PORT} (delay {DELAY_S}s, log {LOG_PATH})",
        flush=True,
    )
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
