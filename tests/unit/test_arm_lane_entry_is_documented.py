"""The aarch64 Lima lane has one entry card, and its VM name is not invented.

`docs/cross-platform-lanes.md` carries the full lane (boundaries, baselines,
rebuild steps); `docs/build-test-deploy-pitfalls.md` A9 carries the entry card
someone reaches for when they only want to *run* the lane -- including what to
do after `tmp/` was cleaned (the cloud image is only needed by `limactl create`;
the host-side cross-build output is recreated by `xbuild.sh`).

The one thing that can go wrong silently is the VM's name: `lima-vm.sh` and
`e2b-sync.sh` each hardcode it, `phase-run.sh` is driven by it, and the docs
quote it. Renaming the VM in one place turns the lane into "no such instance"
at the worst moment, so the name is pinned to be the same in all of them.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
LANE = REPO / "deploy" / "scripts" / "arm-lane"
RECORD = REPO / "docs" / "build-test-deploy-pitfalls.md"

NAME_ASSIGNMENT = re.compile(r'^name=([A-Za-z0-9._-]+)$', re.MULTILINE)


def _recorded_vm_name() -> str:
    text = RECORD.read_text(encoding="utf-8")
    section = text.split("**A9. arm64 真内核车道的入口", 1)
    assert len(section) == 2, (
        "docs/build-test-deploy-pitfalls.md no longer carries the A9 arm-lane card"
    )
    found = re.search(r"`(sandlock-[a-z0-9-]+)`", section[1])
    assert found, "A9 does not name the VM in backticks"
    return found.group(1)


def test_the_vm_recipe_and_its_driver_are_tracked() -> None:
    """A card that points at files the repo does not have is worse than none."""
    for name in ("vm.yaml", "lima-vm.sh", "phase-run.sh", "e2b-sync.sh", "xbuild.sh"):
        assert (LANE / name).is_file(), f"deploy/scripts/arm-lane/{name} is missing"


def test_every_script_agrees_on_the_vm_name_and_the_card_names_it() -> None:
    names = {}
    for script in ("lima-vm.sh", "e2b-sync.sh"):
        found = NAME_ASSIGNMENT.search((LANE / script).read_text(encoding="utf-8"))
        assert found, f"{script} no longer assigns `name=`"
        names[script] = found.group(1)
    assert set(names.values()) == {_recorded_vm_name()}, (
        "the aarch64 lane's VM name drifts between its scripts and the A9 card: "
        f"{names} vs {_recorded_vm_name()!r}"
    )


def test_the_card_keeps_the_commands_that_make_it_usable() -> None:
    """The card is only useful if it carries the four-step loop verbatim."""
    text = RECORD.read_text(encoding="utf-8")
    card = text.split("**A9. arm64 真内核车道的入口", 1)[1]
    for command in (
        "limactl start",
        "limactl stop",
        "deploy/scripts/arm-lane/xbuild.sh",
        "deploy/scripts/arm-lane/lima-vm.sh sync",
        "deploy/scripts/arm-lane/phase-run.sh",
        "deploy/scripts/arm-lane/e2b-sync.sh",
        "limactl create --name",
    ):
        assert command in card, f"A9 lost `{command}`, which is why the card exists"
