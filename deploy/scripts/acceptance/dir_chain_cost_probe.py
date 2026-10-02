#!/usr/bin/env python3
"""What one create's directory-chain walks cost on the shared NAS (Task 5).

``c3_agent.materialize`` opens every destination directory with a from-``/``
walk, one component at a time, ``O_NOFOLLOW`` (design §4.3.1). On the shared
mount -- the shape live today, ``E2B_TREES_SHARED=1`` -- **each component is a
metadata round trip**, and one create asked for the same tree's chain two to
three times (the root's ``fchmod``, the ``workspace/`` under it, and the root
again for the mode pass after a payload lands). Task 5 gave the create one
chain cache per materialization; this probe is that change's before/after
instrument. (A whole create's restore numbers -- the ones in
``docs/deploy-clusters.md`` §7.30 -- come from ``snapshot_create_probe.py``;
this one measures only the part of a create that is directory chains.)

It measures inside the **agent's own container** (the ``maint`` container of
the ``e2b-c3-agent`` pod, where ``E2B_WORKSPACE_BASE`` and the NAS both live),
against a tree a live sandbox created at the production path
``<export>/workspaces/<id>``. The candidate is the ``c3_agent/materialize.py``
of this checkout -- its source is piped in and run as ``__main__`` next to the
*deployed* module, so the two algorithms are timed in one process on one tree,
interleaved, with the same NFS metadata cache behind both:

* ``before_*`` -- the deployed walk. When the deployed module still has the old
  ``_open_dir_chain`` the probe calls *it*; once this ships, it spells the same
  walk out (6 lines) and says so on the ``before_source=`` line, because after
  the fix the deployed module no longer contains it.
* ``after_*`` -- the candidate's ``_VerifiedDirectories`` cache.

Reads the NAS and nothing else: every step is an ``open``/``fstat``/``close``.
The sandbox is created and killed through the public API, so the fleet is left
as it was found.

    deploy/scripts/open-cluster-tunnel.sh          # 通道 + 身份自检（2 节点 / arm64 / +k0s）
    export KUBECONFIG="$PWD/tmp/k0s/kubeconfig"
    export E2B_API_URL=http://172.18.78.49:3000 E2B_SANDBOX_URL=http://172.18.78.49:3000
    export E2B_API_KEY=$(kubectl -n sandlock get secret e2b-secrets \
        -o jsonpath='{.data.E2B_API_KEYS}' | base64 -d | cut -d, -f1)
    env -u http_proxy -u https_proxy -u all_proxy tmp/venv/bin/python \
        deploy/scripts/acceptance/dir_chain_cost_probe.py --n 20

2026-10-02 (`0.1.0-895-gc478ca0`, tree depth 5, one sandbox, ``--n 20``, p50 ms
on each of the two agent pods):

    before_one_walk             6.658 / 6.853   (5 components, ~1.33 ms each)
    before_create              15.470 / 15.913  (the tree's chain, twice)
    before_create_from_a_tar   22.148 / 22.937  (root, subdir, root again)
    after_create                8.848 / 9.228   5 components + 1 re-checked leaf
    after_create_from_a_tar    11.075 / 11.373  ~11 ms off one create
    descriptors_name_the_same_directory=True

The ``after`` side is not zero on purpose: the first walk of a chain still
opens one descriptor per component (there is nothing above the export to cache
across creates), and every later request re-checks its **last** component
``O_NOFOLLOW`` rather than trusting a cached answer about it. Task 3 (trees on
the node-local disk) is what takes the remaining walk off the NAS.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
MODULE = REPO / "c3_agent" / "materialize.py"

#: Appended to the module source and piped into ``python3 -`` on the node, so
#: the candidate's own names (``_VerifiedDirectories``) are in scope while the
#: deployed ``c3_agent.materialize`` is a second, independent module.
SNIPPET = r'''
import os as _os
import statistics as _statistics
import sys as _sys
import time as _time
from pathlib import Path as _Path

import c3_agent.materialize as _live

_N = __N__  # substituted by the caller: `kubectl exec` does not forward its env
_root = _Path(_sys.argv[1])
_subdir = _root / "workspace"
assert _subdir.is_dir(), f"{_subdir} is not a directory"

if hasattr(_live, "_VerifiedDirectories"):
    # The deployed module already has the cache, so the old walk is spelled out
    # here -- identical to what `_open_dir_chain` did before this change.
    def _before(path):
        fd = _os.open("/", _os.O_RDONLY | _os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                child = _live._open_dir_at(part, fd, where=f"opening {path}")
                _os.close(fd)
                fd = child
        except BaseException:
            _os.close(fd)
            raise
        return fd

    _before_source = "spelled-out (the deployed module already has the fix)"
else:
    _before = _live._open_dir_chain
    _before_source = "deployed c3_agent.materialize._open_dir_chain"

# The same counter the unit case uses: a walk from `/` is exactly one
# `open("/", ...|O_DIRECTORY)`, however the rest of the walk is spelled.
_real_open = _os.open
_walks = [0]


def _counting_open(path, flags, *args, **kwargs):
    if flags & _os.O_DIRECTORY and str(path) == "/":
        _walks[0] += 1
    return _real_open(path, flags, *args, **kwargs)


_os.open = _counting_open


def _timed(fn):
    _walks[0] = 0
    started = _time.perf_counter()
    fn()
    return (_time.perf_counter() - started) * 1e3, _walks[0]


def _close(fd):
    _os.close(fd)


def _before_one():
    _close(_before(_root))


def _after_one():
    with _VerifiedDirectories() as directories:
        _close(directories.open_dir(_root))
        _close(directories.open_dir(_root))


def _before_create():
    _close(_before(_root))
    _close(_before(_subdir))


def _after_create():
    with _VerifiedDirectories() as directories:
        _close(directories.open_dir(_root))
        _close(directories.open_dir(_subdir))


def _before_tar_create():
    """The live shape: fchmod root, fchmod subdir, root again after the unpack."""
    _close(_before(_root))
    _close(_before(_subdir))
    _close(_before(_root))


def _after_tar_create():
    with _VerifiedDirectories() as directories:
        _close(directories.open_dir(_root))
        _close(directories.open_dir(_subdir))
        _close(directories.open_dir(_root))


_cases = (
    ("before_one_walk", _before_one),
    ("after_one_walk_plus_leaf", _after_one),
    ("before_create", _before_create),
    ("after_create", _after_create),
    ("before_create_from_a_tar", _before_tar_create),
    ("after_create_from_a_tar", _after_tar_create),
)
_samples = {name: [] for name, _ in _cases}
_walk_counts = {name: set() for name, _ in _cases}
for _ in range(_N):
    for _name, _fn in _cases:
        _elapsed, _count = _timed(_fn)
        _samples[_name].append(_elapsed)
        _walk_counts[_name].add(_count)

print(
    f"python={_sys.version.split()[0]} deployed={_live.__file__}"
    f" candidate=<the piped c3_agent/materialize.py> before_source={_before_source}"
)
print(
    f"tree={_root} depth={len(_root.parts) - 1}"
    f" subdir_depth={len(_subdir.parts) - 1} n={_N}"
)
for _name, _fn in _cases:
    _ordered = sorted(_samples[_name])
    print(
        f"METRIC case={_name} n={_N} walks_from_root={sorted(_walk_counts[_name])}"
        f" min_ms={_ordered[0]:.3f} p50_ms={_statistics.median(_ordered):.3f}"
        f" max_ms={_ordered[-1]:.3f}"
    )
print(
    "METRIC per_component_before_ms="
    f"{_statistics.median(_samples['before_one_walk']) / (len(_root.parts) - 1):.3f}"
)

# The cache must not be a different answer: both sides hand back a descriptor
# that names the same directory.
_old_fd = _before(_subdir)
with _VerifiedDirectories() as _directories:
    _new_fd = _directories.open_dir(_subdir)
_old_info = _os.fstat(_old_fd)
_new_info = _os.fstat(_new_fd)
print(
    "METRIC descriptors_name_the_same_directory="
    f"{(_old_info.st_dev, _old_info.st_ino) == (_new_info.st_dev, _new_info.st_ino)}"
)
_os.close(_old_fd)
_os.close(_new_fd)
'''


def _agent_pods(selector: str) -> list[str]:
    """The c3-agent pods (one per node) that run the materialization."""
    out = subprocess.run(
        [
            "kubectl",
            "-n",
            "sandlock",
            "get",
            "pods",
            "-l",
            selector,
            "-o",
            "jsonpath={.items[*].metadata.name}",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not out:
        raise SystemExit(f"no pod matches {selector} in namespace sandlock")
    return out


def _run_in_pod(pod: str, container: str, tree: str, n: int) -> str:
    """Pipe the candidate module + the probe snippet into the agent container."""
    program = MODULE.read_text(encoding="utf-8") + SNIPPET.replace("__N__", str(n))
    completed = subprocess.run(
        [
            "kubectl",
            "-n",
            "sandlock",
            "exec",
            "-i",
            pod,
            "-c",
            container,
            "--",
            "python3",
            "-",
            tree,
        ],
        input=program,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"the probe failed in {pod}/{container}")
    return completed.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=10, help="samples per case")
    parser.add_argument(
        "--export", default="/var/lib/e2b-sandboxes", help="the shared export root"
    )
    parser.add_argument("--container", default="maint", help="the agent container")
    parser.add_argument(
        "--selector", default="app=c3-agent", help="the c3-agent DaemonSet's labels"
    )
    parser.add_argument("--timeout", type=int, default=900, help="sandbox timeout")
    parser.add_argument("--keep", action="store_true", help="leave the sandbox up")
    args = parser.parse_args()

    from e2b import Sandbox  # imported late: `--help` needs no SDK

    sandbox = Sandbox.create(timeout=args.timeout)
    sandbox_id = sandbox.sandbox_id
    print(f"sandbox_id={sandbox_id} export={args.export}", flush=True)
    try:
        # ``workspace/`` is the tree root's own subdir, and the shape a live
        # create has: the probe needs the chain to exist, not the file.
        sandbox.files.write("workspace/kept.txt", "kept\n")
        tree = f"{args.export}/workspaces/{sandbox_id}"
        for pod in _agent_pods(args.selector):
            print(f"--- {pod} ---", flush=True)
            sys.stdout.write(_run_in_pod(pod, args.container, tree, args.n))
        print(
            f"VERDICT dir-chain-probe sandbox={sandbox_id} pods=ok"
            f" candidate={MODULE}",
            flush=True,
        )
    finally:
        if args.keep:
            print(f"KEPT sandbox {sandbox_id}", flush=True)
        else:
            sandbox.kill()
            print(f"killed {sandbox_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
