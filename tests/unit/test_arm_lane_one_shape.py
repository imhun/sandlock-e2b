"""The arm lane runs one shape (N14 S5).

`E2B_REAL_ROOT=0` (the emulated root) and `E2B_PURE_ROOTFS=off` (N15's identity
root) are retired: `envd_service.config.refuse_retired_root_levers` refuses both
by name at startup. A lane that still runs the 0 arm would be measuring a shape
the app no longer serves -- and worse, the numbers it printed would look like
"the other shape is still fine" while nothing in the fleet exercises it.

Two things can go quietly wrong, so both are pinned here:

* the lane's script could keep accepting `0` (a parameter nobody calls is a
  parameter the next reader assumes is supported), and
* `tests/security/conftest.py` could keep *reading* the retired switches, which
  silently builds the retired shape instead of failing -- that pin lives with
  the other shape cases in `test_pure_rootfs_config.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LANE = REPO / "deploy" / "scripts" / "arm-lane"
ACCEPTANCE = REPO / "deploy" / "scripts" / "acceptance"
X86_SECURITY = LANE / "x86-security.sh"
GATEB_PURE = ACCEPTANCE / "gateB-pure-rootfs.sh"

#: Verbatim, and deliberately a sentence rather than a code: this is the error a
#: caller sees at 3 a.m. when they copy a two-state command out of an old run
#: book, so it has to name the retired value and the one value to pass instead.
X86_ZERO_ARM_REFUSAL = (
    "x86-security: E2B_REAL_ROOT=0 is retired (N14 S5): the real root is the "
    "only shape now, so there is no 0 arm to run -- pass 1"
)

GATEB_ZERO_STATE_REFUSAL = (
    "gateB-pure-rootfs: state 0 is retired (N14 S5): the identity pure root "
    "(E2B_PURE_ROOTFS=off + E2B_REAL_ROOT=0) is refused by name at startup "
    "now, so there is no 0 state to run -- pass 1"
)

#: The *docker env spellings* the lanes used for the two retired values. Prose
#: may still name them (that is how a reader learns they are gone); what may not
#: survive is the `-e` pair that would carry one into a container.
RETIRED_ENV_FLAGS = ("-e E2B_PURE_ROOTFS=off", "-e E2B_REAL_ROOT=0")


def test_the_arm_lane_refuses_the_retired_zero_arm(tmp_path: Path) -> None:
    log = tmp_path / "lane.log"

    done = subprocess.run(
        ["/bin/sh", str(X86_SECURITY), "0", str(log)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        # No docker on the runner: the refusal has to come before anything that
        # would need it, which is also what makes this a script-boundary pin
        # rather than a "the lane failed for some reason" pin.
        env={"PATH": "/usr/bin:/bin"},
    )

    assert done.returncode == 2
    assert done.stderr.strip() == X86_ZERO_ARM_REFUSAL
    assert not log.exists(), "the lane started before it refused the 0 arm"


def test_the_pure_lane_refuses_the_retired_zero_state(tmp_path: Path) -> None:
    log = tmp_path / "lane.log"

    done = subprocess.run(
        ["/bin/sh", str(GATEB_PURE), "0", str(log)],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env={"PATH": "/usr/bin:/bin"},
    )

    assert done.returncode == 2
    assert done.stderr.strip() == GATEB_ZERO_STATE_REFUSAL
    assert not log.exists(), "the lane started before it refused the 0 state"


def test_no_lane_script_still_carries_a_retired_lever_into_the_container() -> None:
    """A lane that names a retired value dies at startup, not in the suite.

    The five scripts below all ran the emulated shape once (`gateB-pure-rootfs`
    as its state 0). They are scanned as a set because the failure mode is a
    copy-paste one: the next lane is written by copying the last one.
    """
    for script in (
        X86_SECURITY,
        GATEB_PURE,
        ACCEPTANCE / "gateA-full.sh",
        ACCEPTANCE / "gateB-full.sh",
        ACCEPTANCE / "x86-run-py.sh",
        ACCEPTANCE / "x86-security-one.sh",
    ):
        text = script.read_text(encoding="utf-8")
        for flag in RETIRED_ENV_FLAGS:
            assert flag not in text, (
                f"{script.relative_to(REPO)} still passes `{flag}` into the "
                "container, where `refuse_retired_root_levers` refuses it"
            )

    # The one accepted value is the 1 arm, and it is still what travels into the
    # container (the app accepts `=1`; only `=0` is a retired lever).
    assert '-e E2B_REAL_ROOT="$real_root"' in X86_SECURITY.read_text(encoding="utf-8")


def test_the_minimal_compose_stack_names_the_profile_instead_of_the_lever() -> None:
    """The dev stack used to run on the identity root because it had no profile.

    `E2B_PURE_ROOTFS=off` was its answer to Docker's default seccomp profile not
    admitting `unshare`; that answer is refused at startup now, so the stack has
    to carry the shipped profile like every other deployment (the file's own
    sibling stacks already do) instead of the retired key.
    """
    compose = (REPO / "deploy" / "compose" / "docker-compose.yml").read_text(
        encoding="utf-8"
    )

    # The *key* is what would reach the process; the comment above it may still
    # name the retired value (that is how a reader learns it is gone).
    assert "E2B_PURE_ROOTFS:" not in compose
    assert "seccomp=${E2B_SECCOMP_PROFILE:-../seccomp/sandlock-worker.json}" in compose
