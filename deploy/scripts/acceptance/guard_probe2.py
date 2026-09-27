"""Post-fix probe: the vacuity hole is now an assertion, not a silent green."""
from __future__ import annotations
import importlib.util
from pathlib import Path

# repo root: this file lives at deploy/scripts/acceptance/guard_probe2.py
REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("guard", REPO / "tests/unit/test_checkpoint_restore_unused.py")
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)

empty = REPO / "tmp/e5e8/empty"
print("empty tree, _worker_sources:", guard._worker_sources(empty))
print("empty tree, _offenders     :", guard._offenders(empty))
saved = guard.REPO
guard.REPO = empty
try:
    guard.test_the_scan_actually_reads_the_worker_tree()
    print("GREEN on empty tree -> NOT FIXED")
except AssertionError:
    print("RED on empty tree -> the non-vacuity assertion fires (fix works)")
finally:
    guard.REPO = saved
guard.test_the_scan_actually_reads_the_worker_tree()
print("GREEN on the real tree -> ok")
