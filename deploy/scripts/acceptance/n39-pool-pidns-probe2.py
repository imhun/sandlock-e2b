"""N39 follow-up, take 2: raw `/proc` view inside a *pooled* sandbox.

Take 1's shell nesting swallowed the output, so this asks the sandbox's own
python for the same facts with no shell quoting in the way:

  * `getpid` -- the sandbox's own pid (a host-level number under the shared
    shape, `1`-ish under its own pid namespace);
  * `proc1` -- `/proc/1/cmdline` (the worker's `python -m envd_service` under
    the shared shape, the sandbox's own process under the fleet shape);
  * `nproc` -- how many numeric `/proc` entries the sandbox can see.
"""

from __future__ import annotations

import base64
import os
import sys

os.environ.setdefault("E2B_API_KEY", "local-key")

from e2b import Sandbox

# Base64-encoded so no shell quoting can mangle the program.
INNER_PROGRAM = r'''
import errno
import os


def see(pid):
    try:
        os.kill(pid, 0)
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))


print("getpid=" + str(os.getpid()))
print("kill1=" + see(1))
print("kill2=" + see(2))
print("kill999999=" + see(999999))
try:
    print("nproc=" + str(len([p for p in os.listdir("/proc") if p.isdigit()])))
except OSError as exc:
    print("nproc=ERR " + repr(exc))
'''

INNER = (
    "python3 -c \"import base64;exec(base64.b64decode('"
    + base64.b64encode(INNER_PROGRAM.encode()).decode()
    + "'))\""
)


def main() -> int:
    sbx = Sandbox.create(api_key=os.environ["E2B_API_KEY"])
    try:
        res = sbx.commands.run(INNER)
        print("RAW-STDOUT-START")
        print(res.stdout, end="")
        print("RAW-STDOUT-END")
        print("RAW-STDERR-START")
        print(res.stderr, end="")
        print("RAW-STDERR-END")
        print("EXIT-CODE=" + str(res.exit_code))
    finally:
        sbx.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
