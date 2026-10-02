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
  and the payload -- ``fs.tar`` (Task 2) or the pre-tar ``fs/`` directory, the
  question ``gateway_common.paths.snapshot_payload`` answers in one place -- are
  present, plus the record's own ``status`` and ``created_at`` -- so a "record
  only" id can be read as *why* it has no payload;
* every id has at most one record and at most one payload (no id was silently
  resolved to one of two copies);
* the old payload root ``<workspaces>/_snapshots`` is gone and ``<workspaces>``
  now holds only sandbox trees;
* the migration journal's **recorded plan is complete on disk**: every
  platform-namespace ``move`` (``_snapshots/**``, ``_migrate``) has its target
  present and its source gone, every ``rmdir`` shell is gone, and the *last*
  run's entries all landed. That is the only honest way to read "merge, never
  overwrite" never had to fire -- a collision raises ``Refuse`` during planning
  (``deploy/scripts/migrate-state-base.sh``, ``build_plan``), **before the
  journal is even opened**, so a refusal leaves no line to grep for. The
  evidence is "the plan that was written down ran to completion", not "there is
  no refusal line".

  The journal holds **two** runs (N27's ``sandbox tree -> workspaces/<id>``
  moves, and N58's ``_snapshots``/``_migrate`` merges). A sandbox tree that was
  moved and whose sandbox was deleted afterwards is legitimately gone, so those
  entries are reported separately (``tree_moves``) instead of being counted as
  failures.

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
import tarfile
from pathlib import Path

def _repo_root() -> Path | None:
    """The checkout root when this runs as a **file**, else ``None``.

    The documented invocation inside the pods is ``python3 - < <this file>``:
    there the interpreter starts in ``/app`` (the repo root) and puts the *cwd*
    on ``sys.path``, so nothing is needed. Running it as ``python3 <path>``
    puts the script's own directory on ``sys.path`` instead, which is why the
    repo root is added here.

    Both shapes have to be handled *and neither may raise*: on Python 3.12 a
    piped script has no ``__file__`` at all, and on 3.14 it is defined as the
    pseudo-name ``<stdin>`` -- where ``parents[3]`` raises ``IndexError``, and
    the probe then dies before printing anything (hit on the cluster
    2026-10-02, the first run of this probe after the guard was added).
    """
    try:
        here = Path(__file__).resolve()  # noqa: F821 - defined when run as a file
    except NameError:
        return None
    if here.name.startswith("<"):  # ``<stdin>``, ``<string>``, ``-c``
        return None
    try:
        return here.parents[3]
    except IndexError:
        return None


_ROOT = _repo_root()
if _ROOT is not None and str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# The store's two payload names and the "which shape is this snapshot" question
# have exactly one home (``gateway_common/paths.py``): this probe runs inside
# the platform's own images (``/app`` is the workdir on all three), and a
# second spelling of ``fs`` here is how a healthy ``fs.tar`` snapshot gets
# filed under "record only" -- the data-defect class -- after Task 2.
from gateway_common.paths import snapshot_payload

RECORD = "snapshot.json"
MARKER = ".complete"

#: The verbs the migration script writes. Anything else is reported by name --
#: but as an *unknown line*, never as a "refusal": see the module docstring.
JOURNAL_VERBS = ("mkdir", "move", "rmdir")

JOURNAL_HEADER = "# state-base-migration-journal"


def classify(entry: Path) -> dict[str, object]:
    record = entry / RECORD
    found = snapshot_payload(entry)
    shape, payload = found if found is not None else (None, None)
    result: dict[str, object] = {
        "id": entry.name,
        "record": record.is_file(),
        "complete": (entry / MARKER).is_file(),
        "payload": shape is not None,
        "payload_shape": shape,
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
    if shape == "dir":
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
    elif shape == "tar":
        # One tar, so "entries" is its member count and "bytes" the file's own
        # size -- the numbers the design document's §4.1 table reports.
        with tarfile.open(payload) as tar:
            result["payload_entries"] = sum(1 for _ in tar)
        result["payload_bytes"] = payload.stat().st_size
    return result


def journal_report(path: Path, root: Path) -> dict[str, object]:
    if not path.is_file():
        return {"present": False, "path": str(path)}
    verbs: dict[str, int] = {}
    unknown_lines: list[str] = []
    runs: list[dict[str, str]] = []
    moves: list[tuple[str, str, int]] = []
    rmdirs: list[tuple[str, int]] = []
    seen_targets: set[str] = set()
    duplicate_targets: list[str] = []
    run_index = -1
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        if line.startswith(JOURNAL_HEADER):
            fields = dict(
                part.split("=", 1) for part in line.split("\t")[1:] if "=" in part
            )
            runs.append(fields)
            run_index += 1
            continue
        if line.startswith("#"):
            continue
        verb, _, rest = line.partition("\t")
        parts = rest.split("\t")
        verbs[verb] = verbs.get(verb, 0) + 1
        if verb not in JOURNAL_VERBS:
            unknown_lines.append(line)
        elif verb == "move" and len(parts) >= 2:
            moves.append((parts[0], parts[1], run_index))
            if parts[1] in seen_targets:
                duplicate_targets.append(parts[1])
            seen_targets.add(parts[1])
        elif verb == "rmdir" and parts:
            rmdirs.append((parts[0], run_index))

    # Two kinds of entry live in this journal and they cannot be judged the
    # same way:
    #
    # * **platform-namespace** moves (`_snapshots/**`, `_migrate`) -- the N58
    #   merge. Nothing deletes those out from under the check, so "the target
    #   is not on disk" really is a half-done merge;
    # * **sandbox-tree** moves (`sbx_*` -> `workspaces/sbx_*`, the N27 run) --
    #   the tree's whole lifecycle is "the sandbox exists". A tree that was
    #   deleted afterwards (the fleet is empty today) is expected to be gone,
    #   so those are reported separately instead of counted as failures.
    def is_tree(rel: str) -> bool:
        return rel.startswith("workspaces/") and rel.rsplit("/", 1)[-1].startswith("sbx_")

    platform_targets_missing = [
        dst for _src, dst, _run in moves if not is_tree(dst) and not (root / dst).exists()
    ]
    sources_remaining = [src for src, _dst, _run in moves if (root / src).exists()]
    shells_remaining = [rel for rel, _run in rmdirs if (root / rel).exists()]
    tree_targets = [dst for _src, dst, _run in moves if is_tree(dst)]
    tree_targets_present = [dst for dst in tree_targets if (root / dst).exists()]
    last_run = len(runs) - 1
    last_run_moves = [(src, dst) for src, dst, run in moves if run == last_run]
    last_run_shells = [rel for rel, run in rmdirs if run == last_run]
    last_missing = [dst for src, dst in last_run_moves if not (root / dst).exists()]
    last_sources = [src for src, dst in last_run_moves if (root / src).exists()]
    last_shells = [rel for rel in last_run_shells if (root / rel).exists()]
    return {
        "present": True,
        "path": str(path),
        "runs": runs,
        "verbs": verbs,
        "moves": len(moves),
        "unknown_lines": unknown_lines,
        "platform_namespace_targets_missing": platform_targets_missing,
        "move_sources_remaining": sources_remaining,
        "rmdir_shells_remaining": shells_remaining,
        "tree_moves": {
            "count": len(tree_targets),
            "targets_present": len(tree_targets_present),
            "note": (
                "sandbox trees moved by the N27 run; a tree whose sandbox was deleted "
                "afterwards is legitimately gone, so only the platform-namespace moves "
                "and the last run are judged strictly"
            ),
        },
        "last_run": {
            "moves": len(last_run_moves),
            "missing_targets": last_missing,
            "sources_remaining": last_sources,
            "shells_remaining": last_shells,
        },
        "recorded_plan_completed": bool(moves)
        and not platform_targets_missing
        and not sources_remaining
        and not shells_remaining
        and not unknown_lines,
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
        "journal": journal_report(journal, root),
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
        has_payload = bool(entry["payload"])
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
        # NOT "we found no refusal line": a collision raises Refuse while the
        # plan is being built, before the journal is opened, so a refusal is
        # invisible in the journal. What can be checked is that the plan the
        # journal *does* record ran to completion on disk (every recorded
        # rename landed, every recorded source is gone, every rmdir'd shell is
        # gone) -- combined with the per-id classes above, that is the evidence
        # that the merge had nothing to refuse.
        "recorded_plan_completed": bool(out["journal"].get("recorded_plan_completed")),
        "refusal_note": (
            "a collision raises Refuse during build_plan and writes no journal "
            "line, so 'no refusal' cannot be grepped for; it is established by "
            "the recorded plan completing on disk plus the four-class id table"
        ),
    }
    json.dump(out, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
