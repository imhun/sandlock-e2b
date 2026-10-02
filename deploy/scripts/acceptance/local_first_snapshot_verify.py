#!/usr/bin/env python3
"""Re-verify the merged ``_snapshots`` store (Task 1 Step 2).

Task 0 merged the two ``_snapshots`` namespaces -- the control plane's *record*
root and the agent's *payload* root -- into one directory per id under the
shared export root, with a ``rename(2)`` per entry and a named refusal if the
two sides ever held the same name. This script is the **independent re-measure**
of that merge, because Task 2's path derivation is built on the claim that one
directory now holds both halves.

What it establishes, from the live volume and not from the migration's own
report:

* per id: which of ``snapshot.json`` (record), ``.complete`` (payload finished)
  and ``fs/`` (payload) are present, plus the record's own ``status`` and
  ``created_at`` -- so a "record only" id can be read as *why* it has no payload;
* every id has at most one record and at most one payload (no id was silently
  resolved to one of two copies);
* the old payload root ``<workspaces>/_snapshots`` is gone and ``<workspaces>``
  now holds only sandbox trees;
* the migration journal carries no refusal/collision line -- i.e. "merge, never
  overwrite" never had to fire on this data.

Read-only. Run it where the shared volume is visible (control-plane or agent)::

    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    kubectl -n sandlock exec -i <cp-pod> -c control-plane -- python3 - \
        < deploy/scripts/acceptance/local_first_snapshot_verify.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

RECORD = "snapshot.json"
MARKER = ".complete"
PAYLOAD = "fs"

#: The journal is written by the migration script; anything that is not one of
#: these verbs is a refusal or an unknown, and both are reported by name.
JOURNAL_VERBS = ("mkdir", "move", "rmdir", "sample", "verify", "summary", "#")


def classify(entry: Path) -> dict[str, object]:
    record = entry / RECORD
    payload = entry / PAYLOAD
    result: dict[str, object] = {
        "id": entry.name,
        "record": record.is_file(),
        "complete": (entry / MARKER).is_file(),
        "payload_dir": payload.is_dir(),
        "payload_entries": None,
        "payload_bytes": None,
        "status": None,
        "created_at": None,
        "sandbox_id": None,
        "node_id": None,
    }
    if result["record"]:
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:  # a torn record is a finding too
            result["record_error"] = f"{type(exc).__name__}: {exc}"
        else:
            result["status"] = data.get("status")
            result["created_at"] = data.get("created_at")
            result["sandbox_id"] = data.get("sandbox_id")
            result["node_id"] = data.get("node_id")
    if result["payload_dir"]:
        entries = 0
        total = 0
        for dirpath, dirnames, filenames in os.walk(payload):
            entries += len(dirnames) + len(filenames)
            for name in filenames:
                try:
                    total += (Path(dirpath) / name).stat().st_size
                except OSError:
                    pass
        result["payload_entries"] = entries
        result["payload_bytes"] = total
    return result


def journal_report(path: Path) -> dict[str, object]:
    if not path.is_file():
        return {"present": False, "path": str(path)}
    verbs: dict[str, int] = {}
    refusals: list[str] = []
    duplicate_targets: list[str] = []
    seen_targets: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip() or line.startswith("# state-base-migration-journal"):
            continue
        if line.startswith("#"):
            continue
        verb, _, rest = line.partition("\t")
        verbs[verb] = verbs.get(verb, 0) + 1
        if verb not in JOURNAL_VERBS:
            refusals.append(line)
        if verb == "move":
            target = rest.split("\t")[-1]
            if target in seen_targets:
                duplicate_targets.append(target)
            seen_targets.add(target)
    return {
        "present": True,
        "path": str(path),
        "verbs": verbs,
        "refusals": refusals,
        "duplicate_move_targets": duplicate_targets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--shared-root", default=os.environ.get("E2B_SHARED_VOLUME_ROOT", "/var/lib/e2b-sandboxes"))
    parser.add_argument("--journal", default=None)
    args = parser.parse_args()

    root = Path(args.shared_root)
    snapshots = root / "_snapshots"
    workspaces = root / "workspaces"
    journal = Path(args.journal) if args.journal else root / "state" / ".state-base-migration.journal"

    out: dict[str, object] = {
        "shared_root": str(root),
        "snapshot_root": str(snapshots),
        "snapshot_root_exists": snapshots.is_dir(),
        "legacy_payload_root": str(workspaces / "_snapshots"),
        "legacy_payload_root_exists": (workspaces / "_snapshots").exists(),
        "workspaces_entries": sorted(p.name for p in workspaces.iterdir()) if workspaces.is_dir() else [],
        "entries": [],
        "classes": {},
        "journal": journal_report(journal),
    }
    if not snapshots.is_dir():
        json.dump(out, sys.stdout, indent=2)
        print()
        return 1

    entries = [classify(p) for p in sorted(snapshots.iterdir()) if p.is_dir()]
    out["entries"] = entries

    classes = {"record+payload": [], "record only": [], "payload only": [], "empty": []}
    for entry in entries:
        has_record = bool(entry["record"])
        has_payload = bool(entry["payload_dir"])
        key = {
            (True, True): "record+payload",
            (True, False): "record only",
            (False, True): "payload only",
            (False, False): "empty",
        }[(has_record, has_payload)]
        classes[key].append(entry["id"])
    out["classes"] = {k: {"count": len(v), "ids": v} for k, v in classes.items()}

    # Ids in the old payload root would mean the merge left a copy behind, and an
    # id in both places is the exact "silently took one" shape this must refuse.
    leftovers: list[str] = []
    legacy = workspaces / "_snapshots"
    if legacy.is_dir():
        leftovers = sorted(p.name for p in legacy.iterdir())
    out["legacy_leftovers"] = leftovers
    duplicates = sorted(set(leftovers) & {e["id"] for e in entries})
    out["ids_in_both_roots"] = duplicates
    out["verdict"] = {
        "merge_lossless": bool(entries) and not duplicates and not leftovers,
        "collision_refusal_fired": bool(out["journal"].get("refusals")),
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
