#!/usr/bin/env python3
"""Sequential write *inside a sandbox* (Task 1 Step 1 ①).

Today the sandbox's ``/workspace`` is the bind mount of ``<workspace base>/<id>``
on the shared NAS, so a write from inside the sandbox is exactly the
"tree on the shared volume" shape. The "tree local" side of the comparison is
run by ``local_first_storage_probe.py`` on the worker's node-local disk, since
the trees are not on local disk yet (Task 3 flips that) -- this wrapper exists so
the sandbox-side row is one command instead of an ad-hoc ``dd``.

Two shapes of the same file are measured:

* ``--seq-mb`` MiB written in 8 MiB chunks with an ``fsync`` every
  ``--fsync-every-mb`` MiB, ``--repeat`` times (the probe's own definition);
* a plain ``dd`` of 1024 MiB, because the default per-sandbox quota
  (``E2B_DEFAULT_DISK_MB=1024``) refuses a full 1 GiB file -- that refusal is a
  finding, not an error, so it is reported and the tail exit code is preserved.

    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \\
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    tmp/venv/bin/python deploy/scripts/acceptance/local_first_sequential_write_probe.py \\
        --seq-mb 1000 --repeat 10

The sandbox is killed before it exits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PROBE = Path(__file__).with_name("local_first_storage_probe.py")
# The SDK resolves file paths against the sandbox user's home (``/home/user``,
# which is the tree root itself); a leading ``/`` is stripped and joined there,
# so an "absolute" upload path lands in the wrong place. Write by bare name.
PROBE_IN_SANDBOX = "/home/user/lf_probe.py"
PROBE_UPLOAD_NAME = "lf_probe.py"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seq-mb", default="1000", help="comma-separated sizes; 1000 fits the 1024 MiB quota")
    parser.add_argument("--repeat", type=int, default=10)
    parser.add_argument("--fsync-every-mb", type=int, default=64)
    parser.add_argument(
        "--settle-s",
        type=float,
        default=10.0,
        help="wait for the NFS quota to free the previous run's file (see the probe)",
    )
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--dd-mb", type=int, default=1024, help="the plain-dd shape (0 disables)")
    args = parser.parse_args()

    from e2b import Sandbox

    result: dict[str, object] = {"seq_mb": args.seq_mb, "repeat": args.repeat, "dd_mb": args.dd_mb}
    sandbox = Sandbox.create(timeout=args.timeout)
    try:
        result["sandbox_id"] = sandbox.sandbox_id
        sandbox.files.write(PROBE_UPLOAD_NAME, PROBE.read_text(encoding="utf-8"))
        result["df_before"] = sandbox.commands.run("df -h /workspace | tail -1").stdout.strip()

        from e2b.sandbox.commands.command_handle import CommandExitException

        try:
            run = sandbox.commands.run(
                "python3 %s --root sandbox:/workspace --skip-small --seq-mb %s "
                "--repeat %d --fsync-every-mb %d --settle-s %s"
                % (
                    PROBE_IN_SANDBOX,
                    args.seq_mb,
                    args.repeat,
                    args.fsync_every_mb,
                    args.settle_s,
                ),
                timeout=args.timeout,
            )
            result["probe_exit_code"] = run.exit_code
            result["probe_stdout"] = run.stdout
            result["probe_stderr"] = run.stderr
        except CommandExitException as exc:
            # A size the quota refuses is a measurement: record it, stay alive.
            result["probe_exit_code"] = exc.exit_code
            result["probe_stdout"] = exc.stdout
            result["probe_stderr"] = exc.stderr

        if args.dd_mb:
            dd = sandbox.commands.run(
                "rm -rf /workspace/_lfbench.* /workspace/big.bin; "
                "dd if=/dev/zero of=/workspace/big.bin bs=1M count=%d conv=fsync 2>&1; echo RC=$?; "
                "df -h /workspace | tail -1" % args.dd_mb,
                timeout=args.timeout,
            )
            result["dd"] = {"exit_code": dd.exit_code, "stdout": dd.stdout}

        sandbox.commands.run("rm -rf /workspace/_lfbench.* /workspace/big.bin")
        result["df_after"] = sandbox.commands.run("df -h /workspace | tail -1").stdout.strip()
    finally:
        sandbox.kill()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
