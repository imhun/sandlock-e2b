"""The N27 probe's own CLI: ``lane`` re-runs this file *inside* the sandbox.

``lane_main`` writes ``Path(__file__).read_text()`` into the sandbox as
``/home/user/n27-checker.py`` and runs that copy with ``in-sandbox``. A
``--scratch`` default computed while the parser is built reads
``Path(__file__).resolve().parents[3]``, which that copy does not have:

* measured 2026-09-28 on the lane, one day after the synthesized root became the
  pure shape's default (so ``/home/user`` is the sandbox's own root and the copy
  really lands three parents deep) -- ``IndexError: 3`` before ``main()`` could
  dispatch, and the lane reported ``FAIL lane: expected exactly one VERDICT and
  one EXIT line, got 0 and 0``;
* the sandbox copy's depth cannot be reproduced with ``tmp_path`` on macOS
  (``/private/var/folders/...`` is four parents deep, so the eager default
  resolves), so the copy is modelled by loading the probe's source with a
  shallow ``__file__``. ``--scratch`` is read by ``lane`` only, and
  ``in-sandbox`` must not depend on the repo's depth.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PROBE = (
    Path(__file__).resolve().parents[2]
    / "deploy"
    / "scripts"
    / "acceptance"
    / "probe_state_base_visibility.py"
)
#: The copy ``lane`` drops inside the sandbox (``lane_main``'s ``dest``):
#: ``/home/user/n27-checker.py``, i.e. three parents. macOS's ``/home`` is a
#: symlink, so resolving that literal deepens it and hides the very bug this
#: pins; the fixture mirrors the sandbox's depth under a prefix that does not
#: exist (nothing to resolve).
SANDBOX_COPY = "/n27-sandbox/user/n27-checker.py"


def _load_probe(module_file: str) -> types.ModuleType:
    """Import the probe with ``__file__`` set to ``module_file``.

    Same trick as running the copy in the sandbox: only ``__file__`` differs, and
    nothing at module import time touches the filesystem.
    """
    module = types.ModuleType("n27_probe_under_test")
    module.__file__ = module_file
    exec(compile(PROBE.read_text(), module_file, "exec"), module.__dict__)
    return module


def test_the_in_sandbox_checker_runs_from_the_sandbox_copy_depth(
    tmp_path, monkeypatch, capsys
) -> None:
    """A fixture whose state base *is* reachable: ``stat`` and ``chain`` both fail.

    The point is the verdict, not the shape (a real sandbox is what makes those
    paths unreachable -- that is the lane's job): the copy has to reach
    ``checker_main`` and answer exactly one verdict plus one exit line.
    """
    # The premise of the fixture: the copy really is shallow enough that an
    # eagerly-evaluated ``parents[3]`` cannot resolve.
    assert len(Path(SANDBOX_COPY).resolve().parents) == 3
    state_base = tmp_path / "state"
    (state_base / "_runtime" / "sbx").mkdir(parents=True)
    probe = _load_probe(SANDBOX_COPY)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            SANDBOX_COPY,
            "in-sandbox",
            "--state-base",
            str(state_base),
            "--workspace",
            str(tmp_path),
        ],
    )
    monkeypatch.chdir(tmp_path)

    assert probe.main() == 1
    assert capsys.readouterr().out.splitlines()[-2:] == [
        "CHECKER-VERDICT stat=FAIL chain=FAIL",
        "CHECKER-EXIT 1",
    ]


def test_the_lane_scratch_default_is_still_the_repo_path() -> None:
    """The default belongs to ``lane`` -- and it has not moved."""
    probe = _load_probe(str(PROBE))

    assert probe._default_lane_scratch() == str(
        PROBE.parents[3] / "tmp" / "k0s" / "scratch" / "n27"
    )
