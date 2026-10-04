#!/usr/bin/env python3
"""Fleet acceptance for N14 S5: the deployed shape really is the real root.

The judgment is the N35 one, and it is why S5 exists at all: a `#!` script
written into the workspace and executed **in the same command** returns its own
output. The emulated root could only refuse that (EACCES once the file's inode
is exec-allowed, ETXTBSY while the mediator still holds the write descriptor),
so `rc=0` is the shape proof. Two more readings:

* a **binary** copied into the workspace and `chmod +x`ed runs too (the other
  half of what only kernel-side resolution buys) -- note the `chmod`: `cp`
  without `-p` lands `0644`, and the kernel then answers EACCES for want of the
  exec bit, which reads exactly like the emulated root's refusal if you skip it
  (measured 2026-10-04, the first cut of this probe made that mistake);
* `mountinfo` inside the sandbox shows the rootfs as `/`, with the workspace and
  the `/dev` nodes bound inside it.

Usage: ``E2B_API_URL=http://<entry>:3000 E2B_API_KEY=<key> python3 \
deploy/scripts/acceptance/probe_real_root_shape.py``. Exit 0 only when every
reading is the real-root one.
"""

from __future__ import annotations

import os
import sys

from e2b import Sandbox

#: The N35 judgment, verbatim: write a script into the workspace and run it in
#: the same command.
SHEBANG = (
    "printf '#!/bin/sh\\necho shebang-ok\\n' > /home/user/s.sh "
    "&& chmod +x /home/user/s.sh && /home/user/s.sh"
)

#: The binary half. `cp` lands 0644, so the chmod is load-bearing.
BINARY = (
    "cp /bin/echo /home/user/e && chmod +x /home/user/e && /home/user/e elf-ok"
)

#: The root reading: the sandbox's own rootfs is `/` (the host's mounts are the
#: ones bound *inside* it), plus uid 0 in the sandbox's user namespace.
MOUNTS = "grep -E ' /home/user | / ' /proc/self/mountinfo | sed 's/ - .*//'; id -u"


def main() -> int:
    api = os.environ["E2B_API_URL"]
    key = os.environ["E2B_API_KEY"]

    sandbox = Sandbox.create(api_url=api, sandbox_url=api, api_key=key, timeout=120)
    try:
        shebang = sandbox.commands.run(SHEBANG)
        binary = sandbox.commands.run(BINARY)
        readings = sandbox.commands.run(MOUNTS)
    finally:
        sandbox.kill()

    print(f"shebang rc={shebang.exit_code} stdout={shebang.stdout.strip()!r}")
    print(f"binary  rc={binary.exit_code} stdout={binary.stdout.strip()!r}")
    print("readings:\n" + readings.stdout.rstrip())

    failures: list[str] = []
    if shebang.exit_code != 0 or shebang.stdout.strip() != "shebang-ok":
        failures.append(f"the shebang case is not the real root: {shebang.stderr!r}")
    if binary.exit_code != 0 or binary.stdout.strip() != "elf-ok":
        failures.append(f"a workspace binary did not run: {binary.stderr!r}")
    # The rootfs is `/` and it is the *only* thing mounted at `/`; the sandbox
    # is uid 0 inside its own user namespace.
    if " / ro," not in readings.stdout:
        failures.append("the sandbox's root does not read as its own rootfs")
    if readings.stdout.rstrip().splitlines()[-1:] != ["0"]:
        failures.append("the sandbox is not uid 0 inside its own namespace")
    if failures:
        for line in failures:
            print(f"FAIL {line}", file=sys.stderr)
        return 1
    print("PASS the deployed shape is the real root")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
