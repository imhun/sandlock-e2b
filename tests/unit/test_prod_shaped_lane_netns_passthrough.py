"""The prod-shaped lane must be able to run the *whole* suite in the deployed shape.

What this pins -- and, just as important, what it is *not* about:

* **Not the netns contract.** ``tests/contract/test_mcp_netns.py`` is
  self-sufficient and has been all along: the runner image bakes
  ``E2B_TEST_NET_ISOLATION=1`` (``deploy/docker/Dockerfile.test-runner:93-96``,
  since ``407a59c`` 2026-09-03), so it was never skipped, and its
  ``_netns_servers()`` starts its own worker with
  ``envd_settings_extra={"enable_net_isolation": True, "fd_inject_connect": True}``.
  Neither of the lane's switches gates or shapes that module.
* **The whole suite's deployment shape.** The in-process control plane / worker
  take their *deployment default* from ``E2B_ENABLE_NET_ISOLATION`` and
  ``E2B_FD_INJECT_CONNECT`` (``envd_service/config.py:142``/``:150``, pinned by
  ``tests/unit/test_net_isolation_config.py``), and ``docker-compose.prod.yml``
  ships both as ``true``. ``deploy/scripts/test-prod-shaped.sh`` forwarded only
  MIRRORS/MEMORY/PIDNS/CACHE, so "run the suite the way the shipped stack runs"
  could not be selected from the script at all -- which is the premise of the
  documented netns-shaped full run (``tmp/prod-shaped-netns-on.log``,
  docs/production-deployment-requirements.md §2.4.5).

The two switches are a pair in the code (``create_app`` refuses the unpaired
shape), so the lane forwards them together and refuses half a pair. This pins
the passthrough in *both* phases (phase 1 root worker, phase 2 uid 65534 worker).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LANE = (REPO / "deploy" / "scripts" / "test-prod-shaped.sh").read_text(
    encoding="utf-8"
)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def test_lane_forwards_the_net_isolation_pair_when_set() -> None:
    # Unset stays unset: the code default is the shared-netns shape.
    assert 'NETNS_ENV=""\n' in LANE
    assert (
        'NETNS_ENV="-e E2B_ENABLE_NET_ISOLATION=${E2B_ENABLE_NET_ISOLATION} '
        "-e E2B_FD_INJECT_CONNECT=${E2B_FD_INJECT_CONNECT} "
        '-e E2B_TEST_NET_ISOLATION=${E2B_TEST_NET_ISOLATION:-1}"\n' in LANE
    )


def test_half_a_pair_is_refused_instead_of_silently_shared() -> None:
    # `create_app` refuses the unpaired shape, so a lane that quietly ran the
    # shared-netns shape while the operator asked for netns would be a lie.
    assert (
        '    echo "!! E2B_ENABLE_NET_ISOLATION and E2B_FD_INJECT_CONNECT must '
        'be set together (create_app refuses the unpaired shape)" >&2\n' in LANE
    )
    assert "    exit 2\n" in LANE


def test_lane_reproduces_the_container_netns_port_window() -> None:
    """The shared-netns wildcard `allowOut` shape needs the low-port window.

    A worker runs inside a container/pod netns, and every runtime opens
    ``net.ipv4.ip_unprivileged_port_start`` there (measured 2026-10-11:
    ``docker run alpine sysctl net.ipv4.ip_unprivileged_port_start`` answers 0
    with no ``sysctls:`` declared). It matters because the shared-netns shape
    binds the per-sandbox wildcard DNS gateway on ``127.0.1.x:53`` *as the
    sandbox's own uid* (the own-identity slot's supervisor), which fails
    ``bind DNS gateway: Permission denied (os error 13)`` once the window is
    shut -- N36. This lane runs ``--network host``, i.e. in the VM's netns,
    whose own value starts at 1024, so the lane has to open it (and say so)
    before its unprivileged runs start.
    """
    assert "ensure_unprivileged_port_window() {\n" in LANE
    assert "\nensure_unprivileged_port_window\n" in LANE
    assert "net.ipv4.ip_unprivileged_port_start" in LANE
    # One escape hatch, so a run can deliberately measure the shut window.
    assert 'LANE_SKIP_PORT_WINDOW' in LANE
    # The write needs a *privileged one-shot*: the measurement above showed the
    # lane's own capset cannot write it (`sysctl: ... Read-only file system`
    # with SYS_ADMIN + NET_ADMIN). The runner container itself must stay
    # unprivileged -- that is the whole point of the lane -- so the `docker run
    # --privileged` belongs to the preflight, not to the pytest container.
    assert "docker run --rm --privileged --network host" in LANE
    assert "--privileged \\\n" not in LANE


def test_both_phases_carry_the_net_isolation_pair() -> None:
    # Two `docker exec` invocations of *the suite*: phase 1 (root worker) and
    # phase 2 (uid 65534). The containers themselves are created once and
    # reused (`lib/lane-container.sh`), which is why the shape env has to ride
    # the *exec* and not the creation: a reused container's creation-time env
    # is whatever the run that created it asked for, while the shape of *this*
    # run has to be the one in this run's argument list.
    # Each phase carries the line in the same argument list as the other
    # forwarded shapes -- right after `$PIDNS_ENV \`, at that list's own
    # indentation. Asserting the structure (which exec owns the line, what it
    # sits behind, how it is indented) keeps the pin without hardcoding a
    # column count that only phase 1 happens to satisfy.
    lines = LANE.splitlines()
    execs = [
        i
        for i, line in enumerate(lines)
        if line.strip().startswith("docker exec")
        and "pytest" in "\n".join(lines[i : i + 40])
    ]
    assert len(execs) == 2

    forwarded = [i for i, line in enumerate(lines) if line.strip() == "$NETNS_ENV \\"]
    assert len(forwarded) == 2

    for index in forwarded:
        anchor = lines[index - 1]
        assert anchor.strip() == "$PIDNS_ENV \\"
        assert _indent(lines[index]) == _indent(anchor)

    owners = {max(run for run in execs if run < index) for index in forwarded}
    assert owners == set(execs)


def test_lane_reuses_its_containers_and_warms_the_first_launch() -> None:
    """Container reuse + one warm-up, for the two costs that dominate a run.

    Measured 2026-10-11 (``tests/security/test_zz_timing.py``): the *first*
    chroot-sandbox launch inside a fresh container waits out two 15 s slot-verb
    timeouts (~31 s) and every later one takes 0.07 s, and the first image
    rootfs extraction for an image costs 2.7 s per *call* (there are 15 call
    sites). Both are fixed costs of starting over, which is why the lane keeps
    its containers, warms the first launch on creation, and hands the rootfs
    cache a stable directory.
    """
    lib = (REPO / "deploy" / "scripts" / "lib" / "lane-container.sh").read_text(
        encoding="utf-8"
    )
    assert '. "$(dirname "$0")/lib/lane-container.sh"' in LANE
    assert 'LANE_CONTAINER-root' in LANE and 'LANE_CONTAINER-nobody' in LANE
    # Reuse is keyed on the running container's *image*, so a rebuilt runner
    # image is never silently used from an older container.
    assert '{{.State.Running}}' in lib
    assert "docker image inspect -f '{{.Id}}'" in lib
    assert 'LANE_RECREATE' in lib
    # A long-lived container needs a PID 1 that reaps: every sandbox the tests
    # start is a descendant of the exec'd pytest, and orphans would pile up.
    assert "--init --network host --name" in lib
    assert "sleep infinity" in lib
    # The warm-up, and the verb deadline it is given (the run itself keeps the
    # documented 15 s).
    assert "lane_container_warmup" in LANE
    # No readiness-deadline override any more: the lane keeps the deployment's
    # 30 s, because the bug that needed it is fixed in the worker
    # (``identity_grant``'s GIL-holding clone3 -- see the lane's own note).
    assert "E2B_SLOT_READY_TIMEOUT_S" not in LANE
    assert "PyDLL" in (
        REPO / "envd_service" / "identity_grant.py"
    ).read_text(encoding="utf-8")
    # Per-run scratch inside the reused container, so a run never inherits
    # another run's uid-pool markers or sandbox trees.
    assert "lane_container_scratch" in LANE
    assert "E2B_TEST_TMP_ROOT=" in LANE
    # ...while the extracted rootfs cache stays outside it: one extraction per
    # image, not one per call.
    assert 'E2B_TEST_ROOTFS_DIR="$LANE_ROOTFS_DIR"' in lib
    assert "E2B_TEST_ROOTFS_DIR" in (
        REPO / "tests" / "security" / "conftest.py"
    ).read_text(encoding="utf-8")
    # ...and the *unprivileged* phase is handed that same directory, prepared
    # root-side in the lib (mkdir + chmod need no capability, so phase 2's
    # `--cap-drop ALL` is untouched). It lives in the lib on purpose: adding a
    # third `docker exec` to the script would break the two-execs-of-the-suite
    # pin above.
    assert "lane_container_prepare_rootfs" in LANE
    assert "lane_container_prepare_rootfs() {" in lib


def test_the_correctness_lane_leaves_the_concurrency_suite_to_its_own_entry() -> None:
    """`--perf` out of the correctness lane, and its own script in.

    The perf suite asks for a 30-50 sandbox burst (``tests/perf/test_latency.py``)
    while an admission-controlled in-process node declares four concurrent
    sandboxes: in the correctness lane it failed every run with
    ``503: No resources available`` -- admission control working, and not the
    subject of either suite. Operator decision, 2026-10-11.
    """
    perf = (REPO / "deploy" / "scripts" / "test-perf-shaped.sh").read_text(
        encoding="utf-8"
    )
    lib = (REPO / "deploy" / "scripts" / "lib" / "lane-container.sh").read_text(
        encoding="utf-8"
    )
    assert "--ignore=tests/perf" in LANE
    assert " --perf " not in LANE.replace("--ignore=tests/perf", "")
    # The perf entry point runs that suite and only that suite, with `--perf`.
    assert "pytest tests/perf --perf" in perf
    # Visibility: the lane's own `-q` progress is block-buffered when it is
    # redirected, which is *why* a running suite can look stuck for minutes.
    assert "-e PYTHONUNBUFFERED=1" in lib
    assert "--durations=25" in LANE
