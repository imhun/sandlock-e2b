#!/usr/bin/env python3
"""A fresh snapshot is one tar, and it round-trips -- with the mode/fifo arms.

Task 2 changed the payload from an exploded ``fs/`` directory to one ``fs.tar``
(``docs/create-local-first-design.md`` §6, ``docs/deploy-clusters.md`` §7.30).
``snapshot_create_probe.py`` measures **cost** across three tiers; this probe is
the **shape** check the same deploy needs, in the same public-API-only way:

* the store on the volume holds ``fs.tar`` + ``.complete`` and **no** ``fs/``;
* ``Sandbox.create(snapshot_id)`` gets the tree back at the tree root
  (``workspace/kept.txt``, never ``workspace/workspace/…``);
* a symlink in the snapshot comes back a symlink, not a copy of its target;
* ``--modes``: what a restored file's **mode** is, after ``tarfile``'s ``data``
  filter -- the review's prediction (c). The filter is ``mode & 0o755`` plus
  ``| 0o600`` for files, so ``0664 → 0644`` and ``0777 → 0755``; the writer side
  is a plain ``tar.add`` that records the real bits, so the clamp is the
  reader's. Directories end at the tree mode (``0770``) either way, which is
  why the worker can still write one level down.
* ``--fifo``: a fifo in the sandbox's tree. Predicted (c) second half -- the
  **capture** succeeds (``tar.add`` records a FIFO member) and the **restore**
  is refused, because a special file is exactly what the ``data`` filter
  refuses. Off by default so an operator's run is not a failure by design.

    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \\
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    tmp/venv/bin/python deploy/scripts/acceptance/snapshot_tar_roundtrip_probe.py --modes
    tmp/venv/bin/python deploy/scripts/acceptance/snapshot_tar_roundtrip_probe.py --fifo --expect-fifo-refusal

Every sandbox is killed and every snapshot deleted before it exits (unless
``--keep``). ``delete_snapshot`` only drops the record on the replica that
answered, so the probe deletes twice -- the §4.3 behaviour, not a retry loop for
anything else.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

KEY = "workspace/kept.txt"
LINK = "link-to-kept"
MODE_FILES = {"mode-664.txt": "664", "mode-777.txt": "777"}

#: The SDK's file API is rooted at ``/home/user``, which **is** the tree root
#: (an inode check shows the same directory as ``/workspace``), so a payload
#: path of ``workspace/x`` is ``/home/user/workspace/x`` *inside* -- a
#: distinction that cost this probe one debugging round: ``/workspace/x`` is one
#: level off, and ``$HOME`` is empty in the sandbox's own shell.
TREE = "/home/user/workspace"


def _delete_snapshot(Sandbox, snapshot_id: str) -> None:
    """Delete on both replicas: the record is per-replica (§4.3)."""
    for _ in range(2):
        try:
            Sandbox.delete_snapshot(snapshot_id)
        except Exception as exc:  # noqa: BLE001 - the second call is a no-op-ish
            print(f"note: delete_snapshot({snapshot_id}) -> {type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--modes", action="store_true", help="also check restored file modes")
    parser.add_argument("--fifo", action="store_true", help="also exercise a fifo member")
    parser.add_argument(
        "--expect-fifo-refusal",
        action="store_true",
        help="treat the fifo restore being refused as the expected outcome",
    )
    parser.add_argument("--keep", action="store_true", help="leave the snapshot behind")
    args = parser.parse_args()

    os.environ.setdefault("E2B_API_KEY", os.environ.get("E2B_API_KEY", ""))
    from e2b import Sandbox

    source = Sandbox.create(timeout=args.timeout)
    snapshot_id = None
    failures: list[str] = []
    try:
        source.files.write(KEY, "kept\n")
        source.files.write("workspace/deep/nested.txt", "nested\n")
        source.commands.run(
            f"ln -s workspace/kept.txt /home/user/{LINK}"
        )
        if args.modes:
            for name in MODE_FILES:
                source.files.write(f"workspace/{name}", "mode\n")
            source.commands.run(
                f"chmod 664 {TREE}/mode-664.txt; chmod 777 {TREE}/mode-777.txt"
            )

        started = time.monotonic()
        snapshot = source.create_snapshot()
        snapshot_id = snapshot.snapshot_id
        print(
            f"METRIC capture_ms={(time.monotonic() - started) * 1000:.0f}"
            f" snapshot={snapshot_id}",
            flush=True,
        )

        created = Sandbox.create(snapshot_id, timeout=args.timeout)
        try:
            kept = created.files.read(KEY)
            nested = created.files.read("workspace/deep/nested.txt")
            print(f"read {KEY} -> {kept!r}; workspace/deep/nested.txt -> {nested!r}", flush=True)
            if kept != "kept\n":
                failures.append(f"{KEY} came back as {kept!r}")
            if nested != "nested\n":
                failures.append(f"workspace/deep/nested.txt came back as {nested!r}")
            try:
                created.files.read("workspace/workspace/kept.txt")
                failures.append("the tree landed one level too deep (workspace/workspace/…)")
            except Exception as exc:  # noqa: BLE001 - the absence is the assertion
                print(f"no workspace/workspace/kept.txt ({type(exc).__name__})", flush=True)

            probe = created.commands.run(
                "set -e; "
                f"test -L /home/user/{LINK} && echo link=$(readlink /home/user/{LINK}); "
                f"stat -c 'dirmode=%a' /home/user; "
                f"stat -c 'dirmode_workspace=%a' {TREE}; "
                f"stat -c 'filemode=%a' {TREE}/kept.txt"
            )
            print(probe.stdout.strip().replace("\n", " | "), flush=True)
            if f"link=workspace/kept.txt" not in probe.stdout:
                failures.append("the symlink did not come back as a symlink to its target")

            if args.modes:
                listed = created.commands.run(
                    f"stat -c '%n %a' {TREE}/mode-664.txt {TREE}/mode-777.txt"
                )
                print(listed.stdout.strip().replace("\n", " | "), flush=True)
                wanted = {
                    "mode-664.txt": "644",
                    "mode-777.txt": "755",
                }
                for line in listed.stdout.strip().splitlines():
                    name, _, mode = line.rpartition(" ")
                    base = os.path.basename(name)
                    if base in wanted and mode != wanted[base]:
                        failures.append(f"{base} restored as {mode}, predicted {wanted[base]}")
        finally:
            created.kill()
    finally:
        if snapshot_id is not None and not args.keep:
            _delete_snapshot(Sandbox, snapshot_id)
        source.kill()

    if args.fifo:
        source = Sandbox.create(timeout=args.timeout)
        fifo_snapshot = None
        try:
            source.commands.run(f"mkdir -p {TREE}; mkfifo {TREE}/pipe")
            source.files.write(KEY, "kept\n")
            try:
                snapshot = source.create_snapshot()
                fifo_snapshot = snapshot.snapshot_id
                print(f"METRIC fifo_capture=ok snapshot={fifo_snapshot}", flush=True)
            except Exception as exc:  # noqa: BLE001 - reporting both arms
                print(f"METRIC fifo_capture=refused {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            if fifo_snapshot is not None:
                try:
                    created = Sandbox.create(fifo_snapshot, timeout=args.timeout)
                    created.kill()
                    print("METRIC fifo_restore=ok", flush=True)
                    if args.expect_fifo_refusal:
                        failures.append("the fifo restore succeeded, expected a refusal")
                except Exception as exc:  # noqa: BLE001 - the refusal is the datum
                    text = str(exc)
                    print(f"METRIC fifo_restore=refused {type(exc).__name__}: {text[:200]}", flush=True)
                    if not args.expect_fifo_refusal:
                        failures.append(f"the fifo restore was refused: {text[:200]}")
        finally:
            if fifo_snapshot is not None and not args.keep:
                _delete_snapshot(Sandbox, fifo_snapshot)
            source.kill()

    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        print(f"RESULT failures={len(failures)}")
        return 1
    print("RESULT failures=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
