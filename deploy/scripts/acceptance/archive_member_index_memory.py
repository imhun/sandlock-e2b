#!/usr/bin/env python3
"""What a payload's **member count** costs the unpack (N63's cap's value).

The unpack streams a member's *data*, but not the member *index*: CPython's
``TarFile.next()`` appends one ``TarInfo`` to ``TarFile.members`` whichever way
the caller iterates, so the cost is O(members) even when every member is empty
-- and the byte cap cannot see it. An archive of 2 000 000 empty members is
~1 GiB of plain 512 B headers, i.e. *inside* ``E2B_TREE_COPY_MAX_BYTES``
(1.25 GiB), with ~0.9 GB of index beside it against a 2 GiB container
(``deploy/k8s/c3-agent.yaml``). ``DEFAULT_ARCHIVE_MAX_MEMBERS`` is 1 500 000;
this probe is where that number stops being a guess.

Every (count, phase) is measured in a **child process**, because ``ru_maxrss``
is a high-water mark that cannot be reset inside one process:

* ``baseline`` -- the child's interpreter, with nothing read;
* ``index``    -- open + walk the tar: the member index on its own (traced);
* ``unpack``   -- ``gateway_common.archive.extract_sandbox_archive`` with the
  cap **disabled** (``max_members=0``): the real path a hostile payload takes;
* ``unpack-no-trace`` -- the same unpack with the tracer off: ``tracemalloc``
  keeps its own tables in real RSS while ``get_traced_memory()`` does not count
  them (measured at 1 500 000 members: 1.64 GiB with the tracer, 0.77 GiB
  without), and the production path never runs a tracer. It is also ~2.5×
  faster, which is what makes the largest size affordable. **This row's
  ``ru_maxrss`` is the process's own RSS and nothing else**: it does not carry
  the kernel's inode/dentry slab or the page cache for the ~1.5 M files the
  unpack just created, so a cgroup comparison has to read ``memory.current``
  (the measure ``docs/create-local-first-design.md`` §3.0 uses), not the
  limit.

The numbers are raw -- members, archive bytes, ``tracemalloc`` peak, peak RSS,
seconds -- and rows print as they land, so an aborted run still has its data.
Scratch lives under the checkout's ``tmp/`` (never ``/tmp``) and is removed
unless ``--keep``::

    .venv/bin/python deploy/scripts/acceptance/archive_member_index_memory.py

Two shapes from ``docs/create-local-first-design.md`` §3.0 are what the value
has to satisfy: the agent's 2 GiB ``maint`` face, and an archive the byte cap
admits. Add a size with ``--sizes`` when a new one needs the same treatment.
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tarfile
import time
import tracemalloc
from pathlib import Path


def _repo_root() -> Path | None:
    """The checkout root when this runs as a **file**, else ``None``.

    Same shape as the sibling probes: the interpreter has to be able to import
    ``gateway_common`` no matter where the probe is invoked from, and a piped
    script (``python3 - < <file>``) has no usable ``__file__`` at all.
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

from gateway_common.archive import extract_sandbox_archive  # noqa: E402

#: ``index`` and ``unpack`` trace allocations (that is where the *index* bytes
#: come from). The trailing ``unpack-no-trace`` is the row whose ``ru_maxrss``
#: is the production path's own memory: ``tracemalloc`` keeps its own
#: bookkeeping in real RSS, and at 285 000 members that was measured at 310 MiB
#: of process RSS against a 124 MB traced index -- an observer that would have
#: been reported as "the payload's memory". It is still only process RSS: the
#: slab and the page cache the created files add are not in it, so compare it
#: against the cgroup's ``memory.current``, never its limit.
DEFAULT_PHASES = ("index", "unpack", "unpack-no-trace")
DEFAULT_SIZES = (200_000, 1_500_000)
DEFAULT_WORK_DIR = "tmp/archive-member-index-memory"
#: What the unpack's peak has to fit: the agent's ``maint`` face.
CONTAINER_LIMIT_BYTES = 2 * 1024**3


def _rss_peak_bytes() -> int:
    """``ru_maxrss`` in bytes (Linux reports KiB, macOS bytes)."""
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * 1024


def build_tar(path: Path, count: int) -> None:
    """Write ``count`` empty members at the tar root (512 B of header each)."""
    staging = path.with_name(path.name + ".part")
    started = time.perf_counter()
    with tarfile.open(staging, "w", bufsize=1024 * 1024) as tar:
        for index in range(count):
            info = tarfile.TarInfo(f"m{index:07d}")
            info.mode = 0o644
            tar.addfile(info)
    os.replace(staging, path)
    print(
        f"built {path} :: {count} empty members, {path.stat().st_size} bytes, "
        f"{time.perf_counter() - started:.1f} s",
        flush=True,
    )


def _phase_baseline() -> dict[str, object]:
    """Nothing is read: the interpreter's own footprint is the reference."""
    return {"members": 0, "traced_peak_bytes": 0, "written": 0}


def _phase_index(tar_path: Path, *, trace: bool) -> dict[str, object]:
    """Open and walk the tar: ``len(tar.members)`` is the index this measures."""
    if trace:
        tracemalloc.start()
    with tarfile.open(tar_path, "r:") as tar:
        for _member in tar:
            pass
        members = len(tar.members)
        traced_peak = tracemalloc.get_traced_memory()[1] if trace else 0
    if trace:
        tracemalloc.stop()
    return {"members": members, "traced_peak_bytes": traced_peak, "written": 0}


def _phase_unpack(tar_path: Path, dest: Path, *, trace: bool) -> dict[str, object]:
    """The production path, cap off: what a hostile tar costs to land."""
    dest.mkdir(parents=True, exist_ok=True)
    if trace:
        tracemalloc.start()
    written = extract_sandbox_archive(tar_path, dest, max_members=0)
    traced_peak = tracemalloc.get_traced_memory()[1] if trace else 0
    if trace:
        tracemalloc.stop()
    return {"members": written, "traced_peak_bytes": traced_peak, "written": written}


def _child_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="one measured phase (internal)")
    parser.add_argument("--phase", required=True)
    parser.add_argument("--count", type=int, default=0)
    parser.add_argument("--tar", default="")
    parser.add_argument("--dest", default="")
    parser.add_argument("--trace", dest="trace", action="store_true", default=True)
    parser.add_argument("--no-trace", dest="trace", action="store_false")
    args = parser.parse_args(argv)

    started = time.perf_counter()
    if args.phase == "baseline":
        row = _phase_baseline()
    elif args.phase == "index":
        row = _phase_index(Path(args.tar), trace=args.trace)
    elif args.phase == "unpack":
        row = _phase_unpack(Path(args.tar), Path(args.dest), trace=args.trace)
    else:  # pragma: no cover - the parent only asks for the three above
        raise SystemExit(f"unknown phase {args.phase!r}")
    row.update(
        {
            "count": args.count,
            "phase": args.phase if args.trace else f"{args.phase}-no-trace",
            "tar_bytes": Path(args.tar).stat().st_size if args.tar else 0,
            "peak_rss_bytes": _rss_peak_bytes(),
            "seconds": round(time.perf_counter() - started, 2),
        }
    )
    print(json.dumps(row), flush=True)
    return 0


def _run_child(extra: list[str]) -> dict[str, object]:
    """One measurement, one fresh process (so ``ru_maxrss`` means one phase)."""
    script = Path(__file__).resolve()
    proc = subprocess.run(
        [sys.executable, str(script), *extra],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


TABLE_HEADER = (
    f"{'members':>9}  {'phase':<16} {'archive_bytes':>13} "
    f"{'traced_peak_bytes':>17} {'traced_bytes/member':>20} "
    f"{'peak_rss_bytes':>14} {'seconds':>8}"
)


def _print_row(row: dict[str, object]) -> None:
    """One row, printed as it is measured: a long run still yields its numbers."""
    count = int(row["count"])
    traced = int(row["traced_peak_bytes"])
    per_member = f"{traced / count:.1f}" if traced and count else "-"
    print(
        f"{count:>9}  {str(row['phase']):<16} {int(row['tar_bytes']):>13} "
        f"{traced:>17} {per_member:>20} {int(row['peak_rss_bytes']):>14} "
        f"{float(row['seconds']):>8.2f}",
        flush=True,
    )


def _print_summary(
    rows: list[dict[str, object]], baseline: dict[str, object]
) -> None:
    base_rss = int(baseline["peak_rss_bytes"])
    index_rows = [row for row in rows if row["phase"] == "index"]
    if index_rows:
        biggest = max(index_rows, key=lambda row: int(row["count"]))
        traced = int(biggest["traced_peak_bytes"])
        count = int(biggest["count"])
        print(
            f"\nat {count} members the index alone is {traced} traced bytes "
            f"({traced / count:.1f} B/member)"
        )
    printed_unpack = False
    for row in sorted(rows, key=lambda row: (int(row["count"]), str(row["phase"]))):
        if not str(row["phase"]).startswith("unpack"):
            continue
        printed_unpack = True
        rss = int(row["peak_rss_bytes"])
        print(
            f"at {row['count']} members {row['phase']} peaked at {rss} RSS "
            f"bytes ({rss / 1024**3:.2f} GiB), {rss - base_rss} bytes over the "
            f"interpreter baseline, of the container's "
            f"{CONTAINER_LIMIT_BYTES / 1024**3:.0f} GiB "
            "(deploy/k8s/c3-agent.yaml)"
        )
    if printed_unpack:
        print(
            "RSS only: this peak is the child's own resident memory and does "
            "not include the kernel's inode/dentry slab or the page cache for "
            "the files the unpack created -- against a cgroup, read "
            "memory.current (docs/create-local-first-design.md §3.0), not the "
            f"{CONTAINER_LIMIT_BYTES / 1024**3:.0f} GiB memory.max above."
        )


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--phase" in argv:  # the child shape; the parent never passes this
        return _child_main(argv)

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--sizes",
        default=",".join(str(size) for size in DEFAULT_SIZES),
        help="member counts to measure, comma separated",
    )
    parser.add_argument(
        "--phases",
        default=",".join(DEFAULT_PHASES),
        help="any of index, unpack, unpack-no-trace; comma separated",
    )
    parser.add_argument("--work-dir", default=DEFAULT_WORK_DIR)
    parser.add_argument("--rebuild", action="store_true", help="rebuild a cached tar")
    parser.add_argument("--keep", action="store_true", help="keep the scratch tree")
    args = parser.parse_args(argv)

    sizes = [int(part) for part in args.sizes.replace(" ", "").split(",") if part]
    phases = [part.strip() for part in args.phases.split(",") if part.strip()]
    work = Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)

    print(TABLE_HEADER)
    print("-" * len(TABLE_HEADER))
    rows: list[dict[str, object]] = []
    baseline = _run_child(["--phase", "baseline"])
    rows.append(baseline)
    _print_row(baseline)

    for count in sizes:
        tar_path = work / f"empty-members-{count}.tar"
        if args.rebuild or not tar_path.is_file():
            build_tar(tar_path, count)
        for phase in phases:
            untraced = phase.endswith("-no-trace")
            base_phase = phase[: -len("-no-trace")] if untraced else phase
            extra = ["--phase", base_phase, "--count", str(count), "--tar", str(tar_path)]
            if untraced:
                extra.append("--no-trace")
            dest = work / f"dest-{count}-{base_phase}{'-no-trace' if untraced else ''}"
            if base_phase == "unpack":
                # A fresh destination every time: extracting over an earlier
                # run's files is a different path in the kernel than creating
                # them, and the row is supposed to be the creating one.
                shutil.rmtree(dest, ignore_errors=True)
                extra += ["--dest", str(dest)]
            row = _run_child(extra)
            rows.append(row)
            _print_row(row)
            if base_phase == "unpack" and not args.keep:
                shutil.rmtree(dest, ignore_errors=True)

    _print_summary(rows, baseline)
    json.dump({"sizes": sizes, "phases": phases, "rows": rows}, sys.stdout, indent=2)
    print()
    if args.keep:
        print(f"scratch kept at {work}", flush=True)
    else:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
