"""Probe the shape guard: does it actually fire, and is it non-vacuous?"""
from __future__ import annotations

import importlib.util
from pathlib import Path

# repo root: this file lives at deploy/scripts/acceptance/guard_probe.py
REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "guard", REPO / "tests/unit/test_checkpoint_restore_unused.py"
)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

real = sorted((REPO / "envd_service").rglob("*.py"))
print(f"real envd_service *.py files scanned: {len(real)}")
try:
    guard.test_no_worker_source_calls_the_fork_checkpoint_restore_api()
    print("REAL TREE: clean (test passed)")
except AssertionError as exc:
    print("REAL TREE: FIRED ->", exc)

for needle in guard.FORBIDDEN:
    root = REPO / "tmp/e5e8/forbid"
    pkg = root / "envd_service"
    pkg.mkdir(parents=True, exist_ok=True)
    # a plausible call site of the binding, spelled exactly as the needle
    (pkg / "bad.py").write_text(f"def f(sb):\n    return sb{needle}1)\n", encoding="utf-8")
    saved = guard.REPO
    guard.REPO = root
    try:
        guard.test_no_worker_source_calls_the_fork_checkpoint_restore_api()
        print(f"needle {needle!r}: NOT caught (BAD)")
    except AssertionError:
        print(f"needle {needle!r}: caught (RED as expected)")
    finally:
        guard.REPO = saved

root = REPO / "tmp/e5e8/empty"
(root / "envd_service").mkdir(parents=True, exist_ok=True)
saved = guard.REPO
guard.REPO = root
try:
    guard.test_no_worker_source_calls_the_fork_checkpoint_restore_api()
    print("EMPTY TREE: silently passes -> vacuous if envd_service vanishes (or has no .py)")
except AssertionError as exc:
    print("EMPTY TREE: fired ->", exc)
finally:
    guard.REPO = saved
