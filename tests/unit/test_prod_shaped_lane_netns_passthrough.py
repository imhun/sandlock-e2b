"""The prod-shaped lane must be able to reproduce the deployed netns shape.

`tests/contract/test_mcp_netns.py` gates itself on `E2B_TEST_NET_ISOLATION=1`
and the worker reads `E2B_ENABLE_NET_ISOLATION` / `E2B_FD_INJECT_CONNECT`, but
`deploy/scripts/test-prod-shaped.sh` forwarded only MIRRORS/MEMORY/PIDNS/CACHE
-- so the documented netns-shaped full run (`tmp/prod-shaped-netns-on.log`,
docs/production-deployment-requirements.md §2.4.5) could not be reproduced
from the script: the netns contract would have been silently skipped instead.
This pins the passthrough in *both* phases (phase 1 root worker, phase 2 uid
65534 worker).
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LANE = (REPO / "deploy" / "scripts" / "test-prod-shaped.sh").read_text(
    encoding="utf-8"
)


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
    # Two `docker run` invocations: phase 1 (root worker, :178-190) and
    # phase 2 (uid 65534, :204-221).
    assert LANE.count("\n    $NETNS_ENV \\\n") == 2
