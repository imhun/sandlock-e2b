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


def test_both_phases_carry_the_net_isolation_pair() -> None:
    # Two `docker run` invocations: phase 1 (root worker) and phase 2 (uid
    # 65534). Each carries the line in the same argument list as the other
    # forwarded shapes -- right after `$PIDNS_ENV \`, at that list's own
    # indentation. Asserting the structure (which run owns the line, what it
    # sits behind, how it is indented) keeps the pin without hardcoding a
    # column count that only phase 1 happens to satisfy.
    lines = LANE.splitlines()
    runs = [i for i, line in enumerate(lines) if line.strip().startswith("docker run ")]
    assert len(runs) == 2

    forwarded = [i for i, line in enumerate(lines) if line.strip() == "$NETNS_ENV \\"]
    assert len(forwarded) == 2

    for index in forwarded:
        anchor = lines[index - 1]
        assert anchor.strip() == "$PIDNS_ENV \\"
        assert _indent(lines[index]) == _indent(anchor)

    owners = {max(run for run in runs if run < index) for index in forwarded}
    assert owners == set(runs)
