"""Is the write-then-exec ETXTBSY window real in the *chroot* (production) shape?

The N15 mediation experiment surfaced it in the pure shape: a mediated write
keeps the mediator's own duplicate of the write descriptor, and the kernel
refuses to exec a file that anyone still holds open for writing. The releaser
is the supervise slot's append-watch tick (`sandlock-supervise/src/events.rs`,
`DEFAULT_INTERVAL = 100 ms`), so the window is one tick -- which means the pair
"write, chmod +x, exec" has to happen inside it. That is exactly what a
user-level install does.

This probe asks the question of the shape we actually run, with the unchanged
code, and uses the pure shape as a negative control (unmediated there, so its
writes register no watch).

Usage (inside the lane container):
    python3 deploy/scripts/acceptance/probe_etxtbsy_shape.py [chroot|pure] [iterations]
"""

import asyncio
import sys
import textwrap

from tests.security.conftest import (
    require_mediation_capable,
    resolve_test_rootfs,
    own_identity_sandbox,
    run_sh,
)


def main() -> int:
    shape = sys.argv[1] if len(sys.argv) > 1 else "chroot"
    iterations = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    if shape == "chroot":
        rootfs = resolve_test_rootfs("python:3.11-slim")
        executor, workspace = own_identity_sandbox("python:3.11-slim", rootfs)
        tool = "/workspace/tool_{i}"
    elif shape == "pure":
        executor, workspace = own_identity_sandbox(None, None)
        tool = "{ws}/tool_{{i}}".format(ws=workspace)
    else:
        raise SystemExit(f"unknown shape {shape!r}")
    try:
        require_mediation_capable(executor)
        script = textwrap.dedent(
            f"""
            set -u
            echo "uid=$(id -u) gid=$(id -g) cwd=$(pwd)"
            ok=0; busy=0; other=0; i=0
            while [ $i -lt {iterations} ]; do
              i=$((i+1))
              f={tool.format(i="$i")}
              printf '#!/bin/sh\\necho hi\\n' > $f
              if [ $i -le 2 ]; then
                echo "before chmod: $(ls -l $f 2>&1)"
              fi
              chmod +x $f
              if [ $i -le 2 ]; then
                echo "after chmod rc=$?: $(ls -l $f 2>&1)"
              fi
              if out=$($f 2>&1); then ok=$((ok+1)); else
                case "$out" in
                  *"Text file busy"*|*"errno 26"*) busy=$((busy+1));;
                  *) other=$((other+1)); echo "OTHER $i: $out";;
                esac
              fi
            done
            echo "RESULT ok=$ok busy=$busy other=$other"
            """
        )
        code, out, err = asyncio.run(run_sh(executor, workspace, script))
        print(f"shape={shape} iterations={iterations} exit={code}")
        print(out.decode(errors="replace").strip())
        tail = err.decode(errors="replace").strip()
        if tail:
            print("stderr tail:", tail[-600:])
        return 0
    finally:
        executor.close()


if __name__ == "__main__":
    raise SystemExit(main())
