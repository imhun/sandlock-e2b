#!/usr/bin/env python3
"""N42 acceptance on the live fleet: does `allow_internet_access=True` reach out?

The decision record (`docs/superpowers/plans/2026-09-26-decisions.md`) says the
deployment side re-tests N42 with "a sandbox created with `allowInternetAccess=True`
can connect out". The shape that must come back:

  * the allowed host (`pypi.org`) resolves and connects -- a bare name reaching
    out is the whole point of the switch;
  * a bare IP is *not* a free pass: with the rule set in place the engine answers
    a policy refusal (that contrast is what makes the first line evidence of a
    working rule rather than of "the network is simply open").

It prints the sandbox's own verdicts, and never prints credentials.
"""

from __future__ import annotations

import os
import shlex
import sys

IN_SANDBOX = r"""
python3 - <<'PY'
import socket, ssl, sys

def try_tcp(host, port=443, timeout=10):
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return f"{host}:{port} ERR {type(exc).__name__} {exc.errno}"
    return f"{host}:{port} CONNECTED"

print("PYPI", try_tcp("pypi.org"))
try:
    ctx = ssl.create_default_context()
    with socket.create_connection(("pypi.org", 443), timeout=10) as s:
        with ctx.wrap_socket(s, server_hostname="pypi.org") as tls:
            print("TLS", tls.version())
except Exception as exc:
    print("TLS ERR", type(exc).__name__, exc)
print("BAREIP", try_tcp("104.20.23.154"))
PY
"""


def main() -> int:
    api_url = os.environ.get("E2B_API_URL")
    api_key = os.environ.get("E2B_API_KEY")
    if not api_url or not api_key:
        print("VACUOUS: E2B_API_URL / E2B_API_KEY missing (never printed)")
        return 2

    from e2b import Sandbox

    print(f"API {api_url}")
    sandbox = Sandbox.create(timeout=600, allow_internet_access=True)
    print(f"SANDBOX {sandbox.sandbox_id} allow_internet_access=True")
    try:
        result = sandbox.commands.run(f"sh -c {shlex.quote(IN_SANDBOX)}", timeout=120)
        print((result.stdout or "").rstrip())
        if result.stderr and result.stderr.strip():
            print("STDERR", result.stderr.rstrip())
        print(f"EXIT {result.exit_code}")
    finally:
        print(f"KILLED {sandbox.sandbox_id} rc={sandbox.kill()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
