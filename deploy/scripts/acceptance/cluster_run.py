#!/usr/bin/env python3
"""N37 cluster run: one command writing N files, on the deployed fleet.

Same shape as ``tmp/plan-2026-09-26/tree_size_bisect.py`` and the F11
acceptance's ``MAKE_TREE`` (one python loop, N files of 512 B), driven through
the proxy the incident used. Prints the sandbox id and per-run timings so the
worker-side log lines can be grepped by sandbox id afterwards.
"""

from __future__ import annotations

import argparse
import os
import time
import traceback


def make_tree(count: int, root: str, sleep_ms: float = 0.0) -> str:
    pace = f"    time.sleep({sleep_ms / 1000.0})\n" if sleep_ms > 0 else ""
    imports = "import os\nimport time\n" if sleep_ms > 0 else "import os\n"
    return (
        "python3 -c \"\n"
        f"{imports}"
        f"root = {root!r}\n"
        "os.makedirs(root, exist_ok=True)\n"
        f"for i in range({count}):\n"
        "    open(os.path.join(root, f'f{i:05d}.bin'), 'wb').write(b'x' * 512)\n"
        f"{pace}"
        "print('created', len(os.listdir(root)))\n"
        "\""
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--files", type=int, default=4000)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--sleep-ms", type=float, default=0.0)
    parser.add_argument("--keep", action="store_true", help="do not kill on failure")
    args = parser.parse_args()

    from e2b import Sandbox

    print(f"api_url={os.environ.get('E2B_API_URL')} files={args.files} runs={args.runs}")
    failures = 0
    for run in range(args.runs):
        sandbox = None
        try:
            created = time.monotonic()
            sandbox = Sandbox.create(timeout=1800)
            print(f"run {run + 1}: sandbox {sandbox.sandbox_id} "
                  f"(create {time.monotonic() - created:.1f}s)", flush=True)
            started = time.monotonic()
            result = sandbox.commands.run(
                make_tree(args.files, f"/home/user/n37-{run}", args.sleep_ms),
                timeout=900,
            )
            took = time.monotonic() - started
            print(f"run {run + 1}: OK after {took:.1f}s exit={result.exit_code} "
                  f"stdout={result.stdout.strip()!r}", flush=True)
            listing = sandbox.commands.run(
                "find /home/user -maxdepth 2 -name 'f*.bin' | wc -l", timeout=120
            )
            print(f"run {run + 1}: files on disk={listing.stdout.strip()}", flush=True)
        except Exception as exc:  # noqa: BLE001 - the failure is the datum
            failures += 1
            print(f"run {run + 1}: FAILED after {time.monotonic() - created:.1f}s "
                  f"{type(exc).__name__}: {str(exc)[:400]}", flush=True)
            traceback.print_exc()
            if args.keep:
                print(f"run {run + 1}: keeping the sandbox", flush=True)
                continue
        finally:
            if sandbox is not None and not (failures and args.keep):
                try:
                    sandbox.kill()
                except Exception as exc:  # noqa: BLE001
                    print(f"run {run + 1}: kill failed: {exc}", flush=True)
    print(f"CLUSTER DONE: {args.runs - failures}/{args.runs} commands succeeded")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
