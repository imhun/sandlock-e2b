"""Every tool image a repo script defaults to is recorded in the pitfalls list.

The sandlock toolchain is a handful of **pre-built local images** that nothing
rebuilds automatically: the fork's canonical `sandlock-dev`, E2B's
`sandlock-dev-f17` (which is really the test-runner recipe under a dev name), the
aarch64 lane's `sandlock-zig-builder`, and E2B's own `e2b-sandlock-test`. Their
existence is not derivable from the tree -- `sandlock-dev` in particular has no
recipe here at all -- so a reader who does not know that will rebuild what
already exists (or delete the one image that cannot be rebuilt).

This pin closes the loop the other way round from the docs: instead of trusting
prose, it reads the image names the scripts actually name and requires each of
them to appear in `docs/build-test-deploy-pitfalls.md` A8. Renaming an image in
a script without recording it here is red; recording it in a doc no script
points at is fine (the table also carries images that are history).
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPTS = REPO / "deploy" / "scripts"
RECORD = REPO / "docs" / "build-test-deploy-pitfalls.md"

#: The tool images this repo's own scripts name. Deliberately narrow: deployment
#: images (`e2b-sandlock-worker`, `.../agent`, ...) are pinned elsewhere and are
#: published artifacts, not pre-built local tooling. Two lookbehinds do that:
#: no word character before the name (so a path segment cannot start a match),
#: and not the `e2b-` prefix (which otherwise makes `e2b-sandlock-quota-agent`
#: look like a `sandlock-quota-agent`). `-` is allowed before it on purpose --
#: `${IMAGE:-sandlock-dev-f17:latest}` is the normal way a script names one.
TOOL_IMAGE = re.compile(
    r"(?<![A-Za-z0-9_.])(?<!e2b-)(sandlock-[a-z0-9][a-z0-9.-]*:[a-z0-9.-]+"
    r"|e2b-sandlock-test:[a-z0-9.-]+)"
)

#: Tags a script handles itself, with one reason each: either it *builds* the
#: tag in the same run (so there is nothing to pre-provision and nothing to
#: record), or it is a deliberate snapshot of one past round. Same shape as
#: `ALLOWED_TMP_REFERENCES` in tests/unit/test_docs_only_point_at_repo_artifacts.py:
#: an allowance has to say why, and one that stops being cited has to be removed.
ALLOWED_SCRIPT_LOCAL_TAGS = {
    "e2b-sandlock-test:c4": (
        "deploy/scripts/c4-prjquota-window.sh 用 Dockerfile heredoc 从目标机上"
        "部署的 worker 镜像现烤，属同轮一次性派生镜像"
    ),
    "e2b-sandlock-test:task12cur": (
        "Task 12 那轮为了不用旧一代的共享 :latest 而烤的快照 tag（该轮探针头部写着"
        "纪律）；2026-09-30 清理本地旧 tag 后已不存在，重跑请显式 "
        "E2B_TEST_IMAGE=e2b-sandlock-test:latest"
    ),
}


def _image_names_used_by_scripts() -> set[str]:
    found: set[str] = set()
    for path in sorted(SCRIPTS.rglob("*.sh")):
        found.update(TOOL_IMAGE.findall(path.read_text(encoding="utf-8")))
    return found


def test_the_recording_section_exists() -> None:
    """A8 is the record; losing it makes every other assertion here vacuous."""
    text = RECORD.read_text(encoding="utf-8")
    assert "**A8. sandlock 相关镜像的清单" in text, (
        "docs/build-test-deploy-pitfalls.md no longer carries the A8 image list"
    )


def test_every_tool_image_a_script_names_is_recorded() -> None:
    used = _image_names_used_by_scripts()
    assert used, "no script names a sandlock tool image any more -- update this pin"
    recorded = RECORD.read_text(encoding="utf-8")
    documented_or_allowed = set(ALLOWED_SCRIPT_LOCAL_TAGS)
    undocumented = sorted(
        name
        for name in used
        if name not in recorded and name not in documented_or_allowed
    )
    assert undocumented == [], (
        "a script defaults to a tool image that the A8 list does not mention, so "
        f"nobody can tell where it comes from or whether to rebuild it: {undocumented}"
    )


def test_every_script_local_tag_allowance_carries_a_reason() -> None:
    bad = sorted(
        name
        for name, reason in ALLOWED_SCRIPT_LOCAL_TAGS.items()
        if not reason.strip() or "\n" in reason
    )
    assert bad == [], f"allowances need a one-line reason each: {bad}"


def test_the_script_scan_actually_sees_the_known_defaults() -> None:
    """Guard the guard: an over-narrow regex would make the pin silently pass."""
    used = _image_names_used_by_scripts()
    expected = {
        f"sandlock-dev-f17:latest",  # deploy/scripts/fork-gate.sh (IMAGE default)
        "sandlock-zig-builder:local",  # deploy/scripts/arm-lane/xbuild.sh
        "e2b-sandlock-test:latest",  # deploy/scripts/build-test-image.sh
    }
    missing = sorted(expected - used)
    assert missing == [], (
        "these tool images are used by scripts but the scan did not find them "
        f"(the pattern or the scripts moved): {missing}"
    )
