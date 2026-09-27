#!/usr/bin/env python3
"""N27 Task 7 acceptance probe: is the platform's state base visible from a sandbox?

What it asserts, inside the sandbox (the "checker" below, mode ``in-sandbox``):

1. ``os.stat`` fails for ``<state_base>``, ``<state_base>/_runtime``,
   ``<state_base>/.route-b`` and ``<state_base>/_runtime/.checkpoints``. Any one
   of them that *succeeds* is a violation. A denial is only accepted when its
   errno is ``ENOENT`` or ``EACCES`` (N15: the pure shape answers with the
   mediator's refusal, the image shape does not have the path at all); any other
   errno is an answer the criterion does not cover and is reported as VACUOUS.
2. The ancestor chain, from ``os.getcwd()`` up to ``/``: every layer must either
   refuse ``os.listdir`` (``ENOENT``/``EACCES``) or list without any of
   ``state``, ``_runtime``, ``.route-b``, ``_secrets`` -- or the basename of the
   state base this run was pointed at (``CHECKER-WATCHED`` names the set, so the
   chain half follows ``--state-base`` the way the stat half does). "It was
   listed" is a violation. Every layer's raw answer (errno or the listing) is
   printed.

A positive control makes both halves falsifiable instead of self-satisfying: the
checker writes a canary into the workspace and requires that it can stat it and
that the workspace layer lists it. If that fails the run is VACUOUS (exit 2) --
"denied everywhere" may not be read as "clean". The criterion also has to be
able to *fail*: ``lane --layout legacy`` runs it against the pre-N27 layout
(platform state in the tree base), where a real sandbox lists ``_runtime`` --
that run is the counter-example, and it comes back exit 1.

Exit 1 has to mean "the checker ran and the criterion failed", so a lane that
cannot run the checker at all (the harness never got a command into the sandbox)
reports VACUOUS (exit 2) rather than letting the interpreter's own failure exit
1 stand in for a counter-example.

Modes
-----

``cluster`` (default)  create a sandbox through the E2B API and run the checker
                       inside it. Credentials come from the environment
                       (``E2B_API_KEY``); they are never printed.
``lane``               run the checker inside an in-process sandbox built the
                       way the worker builds one (``tests.security.conftest``);
                       this is how the shapes the live cluster cannot serve --
                       pure/identity and the synthesized root -- get measured.
                       Must run inside the lane container.
``in-sandbox``         the checker itself; the two modes above invoke the copy
                       they wrote into the sandbox with this argument.

Exit codes: 0 = the assertion holds, 1 = it does not, 2 = VACUOUS.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import sys
from pathlib import Path

#: The names the ancestor chain may never show (the brief's criterion, verbatim).
STATE_NAMES = ("state", "_runtime", ".route-b", "_secrets")
DENIALS = (errno.ENOENT, errno.EACCES)
CANARY = ".n27-probe-canary"


def watched_names(state_base: str) -> tuple[str, ...]:
    """The names the ancestor chain may never show, for *this* state base.

    The brief's four literals, plus the basename of the state base this run was
    pointed at. The stat half already follows ``--state-base``; a chain half
    that watches four hardcoded strings instead goes blind the moment a
    deployment points ``E2B_STATE_BASE`` at a differently named directory --
    ``<export>`` still lists that name, and the probe answers PASS. (Fixture
    pair and the RED/GREEN evidence:
    ``.superpowers/sdd/n27-identity-residual-report.md``.)
    """
    base = os.path.basename(os.path.normpath(state_base))
    names = set(STATE_NAMES)
    if base and base not in ("/", "."):
        names.add(base)
    return tuple(sorted(names))


def _errno_name(exc: OSError) -> str:
    return errno.errorcode.get(exc.errno, str(exc.errno))


# --------------------------------------------------------------------------
# The checker. Runs *inside* the sandbox, stdlib only.
# --------------------------------------------------------------------------


def checker_main(state_base: str, workspace: str) -> int:
    print(f"CHECKER-PWD {os.getcwd()}")
    print(f"CHECKER-STATE-BASE {state_base}")
    print(f"CHECKER-WORKSPACE {workspace}")
    watched = watched_names(state_base)
    print(f"CHECKER-WATCHED {json.dumps(list(watched))}")

    stat_vacuous = False
    chain_vacuous = False
    stat_violation = False
    chain_violation = False

    # --- positive control: the checker can see a file it just wrote ---------
    canary = os.path.join(workspace, CANARY)
    try:
        with open(canary, "w") as handle:
            handle.write("n27\n")
        size = os.stat(canary).st_size
        print(f"CHECKER-CONTROL stat-canary OK size={size}")
    except OSError as exc:
        print(f"CHECKER-CONTROL stat-canary ERR errno={_errno_name(exc)}")
        stat_vacuous = True
    try:
        listing = os.listdir(workspace)
        if CANARY in listing:
            print(f"CHECKER-CONTROL listdir-canary OK entries={len(listing)}")
        else:
            print(f"CHECKER-CONTROL listdir-canary MISSING entries={len(listing)}")
            stat_vacuous = True
    except OSError as exc:
        print(f"CHECKER-CONTROL listdir-canary ERR errno={_errno_name(exc)}")
        stat_vacuous = True

    # --- assertion 1: every platform state path is out of reach ------------
    state_paths = (
        state_base,
        os.path.join(state_base, "_runtime"),
        os.path.join(state_base, ".route-b"),
        os.path.join(state_base, "_runtime", ".checkpoints"),
    )
    for path in state_paths:
        try:
            info = os.stat(path)
        except OSError as exc:
            name = _errno_name(exc)
            print(f"CHECKER-STAT {path} DENIED errno={name}")
            if exc.errno not in DENIALS:
                print(f"CHECKER-STAT {path} UNEXPECTED errno={name}")
                stat_vacuous = True
        else:
            print(f"CHECKER-STAT {path} OK mode={oct(info.st_mode)} ino={info.st_ino}")
            stat_violation = True

    # --- assertion 2: no layer of the ancestor chain shows platform state ---
    layer = os.getcwd()
    layers = 0
    listed_layers = 0
    saw_workspace_layer = False

    def _is_workspace(path: str) -> bool:
        if path == os.getcwd():
            return True
        try:
            return os.path.samefile(path, workspace)
        except OSError:
            return False

    while True:
        layers += 1
        try:
            listing = os.listdir(layer)
        except OSError as exc:
            name = _errno_name(exc)
            print(f"CHECKER-LAYER {layer} DENIED errno={name}")
            if exc.errno not in DENIALS:
                print(f"CHECKER-LAYER {layer} UNEXPECTED errno={name}")
                chain_vacuous = True
        else:
            listed_layers += 1
            print(f"CHECKER-LAYER {layer} LISTED {json.dumps(sorted(listing))}")
            hits = sorted(set(listing) & set(watched))
            if hits:
                print(f"CHECKER-LAYER {layer} LEAK {json.dumps(hits)}")
                chain_violation = True
            if _is_workspace(layer):
                saw_workspace_layer = True
                if CANARY in listing:
                    print(f"CHECKER-LAYER {layer} CANARY-PRESENT")
                else:
                    print(f"CHECKER-LAYER {layer} CANARY-GONE")
                    chain_vacuous = True
        if layer == "/":
            break
        parent = os.path.dirname(layer)
        if parent == layer:
            print(f"CHECKER-CHAIN BROKEN at {layer}")
            chain_vacuous = True
            break
        layer = parent
    if not saw_workspace_layer:
        print("CHECKER-CHAIN NO-WORKSPACE-LAYER")
        chain_vacuous = True
    if listed_layers == 0:
        print("CHECKER-CHAIN NO-LISTED-LAYER")
        chain_vacuous = True
    print(f"CHECKER-CHAIN layers={layers} listed={listed_layers} reached-root=yes")
    try:
        os.unlink(canary)
    except OSError:
        pass

    stat_verdict = "FAIL" if stat_violation else ("VACUOUS" if stat_vacuous else "PASS")
    chain_verdict = (
        "FAIL" if chain_violation else ("VACUOUS" if chain_vacuous else "PASS")
    )
    print(f"CHECKER-VERDICT stat={stat_verdict} chain={chain_verdict}")
    if stat_violation or chain_violation:
        code = 1
    elif stat_vacuous or chain_vacuous:
        code = 2
    else:
        code = 0
    print(f"CHECKER-EXIT {code}")
    return code


# --------------------------------------------------------------------------
# cluster mode: a real sandbox, through the public API
# --------------------------------------------------------------------------


def _state_base_for_cluster(explicit: str | None) -> tuple[str | None, str]:
    """Where the platform's state lives, as the *deployment* names it.

    Returns ``(path, source)``; ``source`` is printed as evidence, and ``None``
    means the probe cannot know where to look (a VACUOUS run, not a pass).
    """
    if explicit:
        return explicit, "--state-base"
    from_env = os.environ.get("E2B_STATE_BASE")
    if from_env:
        return from_env, "E2B_STATE_BASE"
    kubeconfig = os.environ.get("KUBECONFIG")
    if kubeconfig and Path(kubeconfig).exists():
        import subprocess

        out = subprocess.run(
            [
                "kubectl",
                "-n",
                "sandlock",
                "get",
                "sts",
                "e2b-worker",
                "-o",
                "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='E2B_STATE_BASE')].value}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip(), f"kubectl(sts/e2b-worker via {kubeconfig})"
    return None, "unset"


#: The worker's own shape switches: what the deployment *declares* (read-only,
#: from the StatefulSet), so the observed answers can be read against it.
SHAPE_ENV = ("E2B_BASE_IMAGE", "E2B_REAL_ROOT", "E2B_PURE_ROOTFS", "E2B_STATE_BASE")


def _deployment_shape_env() -> dict[str, str] | None:
    kubeconfig = os.environ.get("KUBECONFIG")
    if not kubeconfig or not Path(kubeconfig).exists():
        return None
    import json
    import subprocess

    out = subprocess.run(
        ["kubectl", "-n", "sandlock", "get", "sts", "e2b-worker", "-o", "json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        return None
    workload = json.loads(out.stdout)
    env = {
        entry["name"]: entry.get("value")
        for entry in workload["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    return {name: env.get(name, "<unset>") for name in SHAPE_ENV}


def _observed_pathspace(stdout: str) -> str:
    """What the raw stat answers say about the sandbox's path space."""
    errnos = [
        line.rsplit("errno=", 1)[1]
        for line in stdout.splitlines()
        if line.startswith("CHECKER-STAT ") and " DENIED " in line
    ]
    if len(errnos) == 4 and set(errnos) == {"ENOENT"}:
        return "the state base's path is absent from this root (ENOENT x4)"
    if len(errnos) == 4 and set(errnos) == {"EACCES"}:
        return "the path is there but the mediator refuses it (EACCES x4)"
    if errnos:
        return f"mixed/unexpected denials: {errnos}"
    return "no denial recorded (a stat succeeded -- see the OK lines)"


def _verdict_from(stdout: str, exit_code: int, label: str) -> int:
    """Cross-check the checker's own verdict line against its exit code."""
    verdicts = [line for line in stdout.splitlines() if line.startswith("CHECKER-VERDICT")]
    exits = [line for line in stdout.splitlines() if line.startswith("CHECKER-EXIT")]
    if len(verdicts) != 1 or len(exits) != 1:
        print(
            f"FAIL {label}: expected exactly one VERDICT and one EXIT line, got "
            f"{len(verdicts)} and {len(exits)}"
        )
        return 2
    if exits[0] != f"CHECKER-EXIT {exit_code}":
        print(f"FAIL {label}: exit code {exit_code} != {exits[0]!r}")
        return 2
    halves = dict(
        pair.split("=") for pair in verdicts[0].removeprefix("CHECKER-VERDICT ").split()
    )
    if exit_code == 0 and (halves["stat"], halves["chain"]) != ("PASS", "PASS"):
        print(f"FAIL {label}: {verdicts[0]!r} does not match exit 0")
        return 2
    if exit_code == 1 and "FAIL" not in halves.values():
        print(f"FAIL {label}: {verdicts[0]!r} does not match exit 1")
        return 2
    if exit_code == 2 and "FAIL" in halves.values():
        print(f"FAIL {label}: {verdicts[0]!r} does not match exit 2")
        return 2
    print(f"{label}: {verdicts[0]} (exit {exit_code})")
    return exit_code


def cluster_main(args: argparse.Namespace) -> int:
    import httpx
    from e2b import Sandbox

    api_url = os.environ.get("E2B_API_URL")
    sandbox_url = os.environ.get("E2B_SANDBOX_URL")
    api_key = os.environ.get("E2B_API_KEY")
    missing = [
        name
        for name, value in (
            ("E2B_API_URL", api_url),
            ("E2B_SANDBOX_URL", sandbox_url),
            ("E2B_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        print(
            f"VACUOUS: missing {', '.join(missing)} (credentials come from the "
            "cluster Secret; nothing is printed here)"
        )
        return 2
    state_base, source = _state_base_for_cluster(args.state_base)
    if state_base is None:
        print(
            "VACUOUS: the state base is unknown: pass --state-base, or set "
            "E2B_STATE_BASE, or point KUBECONFIG at the cluster so it can be read "
            "from sts/e2b-worker"
        )
        return 2
    print(f"CLUSTER api={api_url} state-base={state_base} source={source}")
    declared = _deployment_shape_env()
    if declared is None:
        print("CLUSTER deployment-declared shape: unknown (no usable KUBECONFIG)")
    else:
        print(f"CLUSTER deployment-declared shape: {declared}")

    checker = Path(__file__).read_text()
    sandbox = Sandbox.create(timeout=600)
    try:
        print(f"CLUSTER sandbox={sandbox.sandbox_id}")
        detail = httpx.get(
            f"{api_url}/sandboxes/{sandbox.sandbox_id}",
            headers={"X-API-Key": api_key},
            timeout=30,
        )
        detail.raise_for_status()
        record = detail.json()
        # Names only: the detail body carries the sandbox's own configuration,
        # and this probe is not the place to print it.
        print(
            f"CLUSTER record templateID={record.get('templateID')!r} "
            f"keys={sorted(record)}"
        )
        route = httpx.get(
            f"{api_url}/internal/routes/{sandbox.sandbox_id}",
            headers={"X-Internal-Key": os.environ.get("E2B_INTERNAL_API_KEY", "")},
            timeout=30,
        )
        if route.status_code == 200:
            print(f"CLUSTER route={route.json()}")
        sandbox.files.write("n27-probe/checker.py", checker)
        result = sandbox.commands.run(
            f"python3 /home/user/n27-probe/checker.py in-sandbox "
            f"--state-base {state_base} --workspace /home/user",
            timeout=300,
        )
        print("----- checker stdout -----")
        print(result.stdout, end="")
        if result.stderr:
            print("----- checker stderr -----")
            print(result.stderr, end="")
        print(f"----- checker exit={result.exit_code} -----")
        print(f"CLUSTER observed-pathspace: {_observed_pathspace(result.stdout)}")
        return _verdict_from(result.stdout, result.exit_code, "cluster")
    finally:
        try:
            sandbox.kill()
        except Exception as exc:  # noqa: BLE001
            print(f"WARN: kill failed: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# lane mode: the shapes the live cluster cannot serve
# --------------------------------------------------------------------------

LANE_SHAPES = {
    # name: (E2B_PURE_ROOTFS, E2B_REAL_ROOT, what it stands for)
    "identity": ("off", "0", "pure, N15 identity translation, emulated root"),
    "synth-emulated": ("synth", "0", "pure, N16 synthesized root, emulated root"),
    "synth-realroot": ("synth", "1", "pure, N16 synthesized root, real root"),
}


def _vacuous_unreachable(exc: BaseException) -> int:
    """The harness could not run the checker at all -- report VACUOUS (2).

    Never let this leave as exit 1. The contract on this probe is "the legacy
    layout is the counter-example and it comes back 1"; a lane whose sandbox
    died before the checker ran exits 1 too, so an operator (or a script)
    reading only the exit code would read a crash as a working counter-example.
    This is the same code the other "cannot measure" paths use.
    """
    print(f"LANE VACUOUS: the checker never ran ({type(exc).__name__}: {exc})")
    return 2


def _default_lane_scratch() -> str:
    """``<repo>/tmp/k0s/scratch/n27`` -- ``lane``'s fixtures when unset.

    Computed *here* instead of while the parser is built: ``lane`` re-runs this
    file from inside the sandbox (``lane_main`` writes it there as
    ``/home/user/n27-checker.py``), where ``parents[3]`` is an ``IndexError``,
    and an eager default killed that copy before ``main()`` could dispatch --
    measured 2026-09-28, once the synthesized root became the pure shape's
    default (the copy then really lands three parents deep). Only ``lane`` reads
    the value; ``tests/unit/test_n27_probe_cli.py`` pins both halves.
    """
    return str(Path(__file__).resolve().parents[3] / "tmp/k0s/scratch/n27")


def lane_main(args: argparse.Namespace) -> int:
    import asyncio
    import shutil

    repo = Path(__file__).resolve().parents[3]
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from tests.security.conftest import (
        SANDBOX_UID,
        make_sandbox_visible,
        route_b_sandbox,
        run_sh,
    )

    pure_rootfs, real_root, label = LANE_SHAPES[args.shape]
    os.environ["E2B_PURE_ROOTFS"] = pure_rootfs
    os.environ["E2B_REAL_ROOT"] = real_root
    os.environ["E2B_BASE_IMAGE"] = ""

    scratch = Path(args.scratch or _default_lane_scratch())
    root = scratch / f"{args.shape}-{args.layout}"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    if args.layout == "legacy":
        # Pre-N27: the platform's own namespaces sit in the tree base itself.
        state_base = root
        workspace = root / "sbx_probe"
    else:
        # N27: tree root sunk one level, the state base its sibling.
        state_base = root / "state"
        workspace = root / "workspaces" / "sbx_probe"
    # The export root as the manifests build it: the platform's namespaces
    # beside the state base and the (sunk) tree root.
    for name in (
        "_builds",
        "_images",
        "_secrets",
        "_snapshots",
        "_templates",
        "_volumes",
    ):
        (root / name).mkdir(parents=True, exist_ok=True)
    if args.layout == "n27":
        # The deployment keeps the migration's export shell next to the trees
        # (mode 1777, `<id>.tar.gz` files). Not one of the four names the
        # criterion watches, but it belongs in the fixture so "the tree base is
        # clean" is read against what is really there.
        (root / "workspaces" / "_migrate").mkdir(parents=True, exist_ok=True)
        os.chmod(root / "workspaces" / "_migrate", 0o1777)
    for path in (
        state_base / "_runtime" / "sbx_probe",
        state_base / "_runtime" / ".checkpoints" / "sbx_probe",
        state_base / ".route-b",
    ):
        path.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    make_sandbox_visible(workspace)
    for entry in (root, workspace):
        os.chmod(entry, 0o755)
        if os.geteuid() == 0:
            os.chown(entry, SANDBOX_UID, SANDBOX_UID)
    if args.layout == "legacy":
        os.chmod(state_base / "_runtime", 0o700)
        os.chmod(state_base / ".route-b", 0o700)

    checker = Path(__file__).read_text()
    print(
        f"LANE shape={args.shape} ({label}) layout={args.layout}\n"
        f"LANE state-base={state_base} workspace={workspace}"
    )

    async def run() -> int:
        executor, ws = route_b_sandbox(
            None, None, workspace=workspace, host_uid=SANDBOX_UID
        )
        try:
            print(
                f"LANE route_b_active={executor._route_b_active} "
                f"decline={executor._route_b_decline} "
                f"has_root={executor._has_sandbox_root} "
                f"chroot={executor._chroot_root}"
            )
            # The sandbox reaches the checker through its workspace alias.
            dest = Path(ws) / "n27-checker.py"
            dest.write_text(checker)
            if os.geteuid() == 0:
                os.chown(dest, SANDBOX_UID, SANDBOX_UID)
            command = (
                "python3 /home/user/n27-checker.py in-sandbox "
                f"--state-base {state_base} --workspace /home/user"
            )
            try:
                code, out, err = await run_sh(executor, ws, command)
            except Exception as exc:  # noqa: BLE001
                # The host workspace path is not reachable from this shape's
                # root; start from the alias instead, and say so.
                print(f"LANE cwd-retry=/home/user (host path refused: "
                      f"{type(exc).__name__}: {exc})")
                try:
                    code, out, err = await run_sh(executor, "/home/user", command)
                except Exception as retry_exc:  # noqa: BLE001
                    return _vacuous_unreachable(retry_exc)
            if code == 125:
                print("LANE cwd-retry=/home/user (host workspace path refused)")
                try:
                    code, out, err = await run_sh(executor, "/home/user", command)
                except Exception as retry_exc:  # noqa: BLE001
                    return _vacuous_unreachable(retry_exc)
            print("----- checker stdout -----")
            print(out.decode(errors="replace"), end="")
            if err:
                print("----- checker stderr -----")
                print(err.decode(errors="replace"), end="")
            print(f"----- checker exit={code} -----")
            return _verdict_from(out.decode(errors="replace"), code, "lane")
        finally:
            executor.close()

    return asyncio.run(run())


def main() -> int:
    parser = argparse.ArgumentParser(description="N27 state-base visibility probe")
    sub = parser.add_subparsers(dest="mode")

    cluster = sub.add_parser("cluster", help="a live sandbox, through the API")
    cluster.add_argument("--state-base", default=None)
    cluster.set_defaults(func=cluster_main)

    lane = sub.add_parser("lane", help="in-process sandbox (run inside the lane)")
    lane.add_argument("--shape", choices=sorted(LANE_SHAPES), required=True)
    lane.add_argument("--layout", choices=("n27", "legacy"), required=True)
    # No eager default: see ``_default_lane_scratch`` (the sandbox copy of this
    # file is too shallow for ``parents[3]``). ``N27_SCRATCH_DIR`` still wins.
    lane.add_argument(
        "--scratch",
        default=os.environ.get("N27_SCRATCH_DIR"),
    )
    lane.set_defaults(func=lane_main)

    inner = sub.add_parser("in-sandbox", help="the checker itself")
    inner.add_argument("--state-base", required=True)
    inner.add_argument("--workspace", default="/home/user")
    inner.set_defaults(func=None)

    args = parser.parse_args()
    if args.mode is None:
        args = parser.parse_args(["cluster"])
    if args.mode == "in-sandbox":
        return checker_main(args.state_base, args.workspace)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
