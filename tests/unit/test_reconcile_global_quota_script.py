"""The N78 operator script's two decision points, without a cluster.

The live repair is an operator action; what can be pinned here is that the
script defaults to a **dry run** (an accidental write against the fleet ledger
is not a mistake to discover afterwards) and that it refuses by name when there
is no shared ledger to reconcile.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO_ROOT / "deploy" / "scripts" / "reconcile-global-quota.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("reconcile_global_quota", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


SCRIPT_MODULE = _load_script()


def test_the_default_is_a_dry_run():
    args = SCRIPT_MODULE.parse_args([])

    assert args.apply is False
    assert args.json is False
    assert SCRIPT_MODULE.parse_args(["--apply"]).apply is True


def test_the_script_refuses_without_a_redis_url(monkeypatch, capsys):
    monkeypatch.delenv("E2B_REDIS_URL", raising=False)

    assert SCRIPT_MODULE.main([]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "reconcile-global-quota: refusing to run -- E2B_REDIS_URL is not set, "
        "and the in-process ledger cannot drift this way (there is nothing "
        "shared to reconcile)\n"
    )
