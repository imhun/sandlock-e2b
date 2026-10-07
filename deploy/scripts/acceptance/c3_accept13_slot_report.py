#!/usr/bin/env python3
"""What the slot's child *is*, once the agent has written its identity map.

This is the command the harness execs through the production child program
(``python3 -m envd_service.identity_grant --uid X --unshared-fd N -- <this>``),
so the identity it reports is the one the shipped path really produces -- not
one this rig arranged.

Two facts are printed, and both are needed by the acceptance:

* the effective identity (``/proc/self/status``): Uid *and* Gid, because a
  slot's documents are ``owner=<worker>, group=<slot uid>, mode 0440`` and the
  group half is what lets ``sandlock-supervise`` read ``policy.json``;
* which of the pre-created ``policy-<uid>.json`` probes (same ownership and
  mode as a real slot document) this process can open. A slot whose group half
  was set reads exactly the probe whose group is its own uid; one whose group
  half was not set reads none of them.
"""

from __future__ import annotations

import os
import sys
import time


def _ids() -> str:
    wanted = ("Uid", "Gid", "Groups", "NSpid")
    found: dict[str, str] = {}
    with open("/proc/self/status", encoding="utf-8") as handle:
        for line in handle:
            name, _, value = line.partition(":")
            if name in wanted:
                found[name] = value.strip()
    return " ".join(f"{name}={found.get(name, '?')}" for name in wanted)


def main() -> int:
    tag = sys.argv[1]
    probe_dir = sys.argv[2]
    print(f"[{tag}] REPORT-IDS {_ids()}", flush=True)
    try:
        names = sorted(
            name
            for name in os.listdir(probe_dir)
            if name.startswith("policy-") and name.endswith(".json")
        )
    except OSError as exc:
        print(f"[{tag}] REPORT-POLICY dir refused: {exc.strerror}", flush=True)
        return 0
    readable: list[int] = []
    for name in names:
        try:
            with open(os.path.join(probe_dir, name), encoding="utf-8") as handle:
                handle.read()
        except OSError:
            continue
        readable.append(int(name[len("policy-") : -len(".json")]))
    candidates = [int(n[len("policy-") : -len(".json")]) for n in names]
    print(
        f"[{tag}] REPORT-POLICY readable-groups={readable} candidates={candidates}",
        flush=True,
    )
    time.sleep(600.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
