"""Re-embed `deploy/seccomp/sandlock-worker.json` into the ConfigMap installer.

`deploy/k8s/seccomp-installer.yaml` carries the profile as a block scalar plus a
`checksum/profile` annotation, and `tests/unit/test_worker_manifest_permissions.py`
pins both byte-for-byte. The two N35 profile edits skipped that step, so the
DaemonSet would have kept shipping an old filter to every node while the docs
said to apply the new one (measured: the embedded payload is the profile as of
`f532e39~1`).

    python3 deploy/scripts/acceptance/sync-seccomp-installer.py            # dry run (self-check + report)
    python3 deploy/scripts/acceptance/sync-seccomp-installer.py --write

The self-check is a round trip: re-encoding the payload the file carries today
must reproduce the block body byte-for-byte, which is what makes the encoder
trustworthy before it rewrites anything.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INSTALLER = REPO / "deploy" / "k8s" / "seccomp-installer.yaml"
PROFILE = REPO / "deploy" / "seccomp" / "sandlock-worker.json"
# The block scalar is `|` (clip), not `|-` (strip): the profile file ends with a
# newline and the ConfigMap value has to carry it. The script said `|-` until
# 2026-09-30, which made it raise IndexError instead of re-embedding -- the two
# N35 edits it documents had been done by hand, so nobody noticed. The
# round-trip self-check below is what proves the two sides agree again.
MARKER = "  sandlock-worker.json: |\n"
END = "\n---\napiVersion: apps/v1"
INDENT = "    "


def payload_for(profile_text: str) -> str:
    """The ConfigMap body for a profile, exactly as the installer indents it."""
    return "\n".join(
        INDENT + line if line else line for line in profile_text.split("\n")
    )


def embedded(installer_text: str) -> str:
    body = installer_text.split(MARKER, 1)[1].split(END, 1)[0]
    return "\n".join(
        line[4:] if line.startswith(INDENT) else line for line in body.split("\n")
    )


def with_payload(installer_text: str, payload: str) -> str:
    head, rest = installer_text.split(MARKER, 1)
    _old, tail = rest.split(END, 1)
    return f"{head}{MARKER}{payload}{END}{tail}"


def with_checksum(installer_text: str, digest: str) -> str:
    return re.sub(
        r'(        checksum/profile: ")[0-9a-f]{64}(")',
        rf"\g<1>{digest}\g<2>",
        installer_text,
    )


def with_comment_hash(installer_text: str, digest: str) -> str:
    """Refresh the human-readable hash in the file's own header comment.

    `tests/unit/test_worker_manifest_permissions.py` pins this line because it
    drifted once already: the N35 resync bumped the payload and the annotation
    but left the comment on the pre-N35 hash, so the one line a human reads
    named a profile no manifest shipped. Updating it here is what keeps the two
    in lockstep instead of relying on whoever runs the sync to remember.
    """
    return re.sub(
        r"(# tests/unit/test_worker_manifest_permissions\.py\); sha256 )[0-9a-f]{64}",
        rf"\g<1>{digest}",
        installer_text,
    )


def _stale_revision(payload: str) -> str | None:
    """Which revision's profile the payload currently matches, if any."""
    revisions = subprocess.run(
        ["git", "log", "--format=%H", "--", "deploy/seccomp/sandlock-worker.json"],
        cwd=REPO, capture_output=True, text=True, check=True,
    ).stdout.split()
    for sha in revisions:
        text = subprocess.run(
            ["git", "show", f"{sha}:deploy/seccomp/sandlock-worker.json"],
            cwd=REPO, capture_output=True, text=True, check=True,
        ).stdout
        if text == payload:
            return sha[:7]
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()

    installer = INSTALLER.read_text(encoding="utf-8")
    profile = PROFILE.read_text(encoding="utf-8")

    body = installer.split(MARKER, 1)[1].split(END, 1)[0]
    if payload_for(embedded(installer)) != body:
        print("encoder check FAILED: re-encoding the payload on disk does not "
              "reproduce the block body")
        return 1
    print("encoder ok: the payload on disk round-trips through the encoder")

    stale = _stale_revision(embedded(installer))
    print(f"embedded payload is the profile as of revision {stale or '<unknown>'}")

    new_payload = payload_for(profile)
    # The annotation hashes the profile *text* (the file the node ends up with,
    # which the byte-exact test pins as equal to the payload). Verified against
    # the revision the installer was last in sync on: 8ea5909's profile text
    # hashes to the 0e0796... the annotation carried before this sync.
    digest = hashlib.sha256(profile.encode()).hexdigest()
    updated = with_comment_hash(
        with_checksum(with_payload(installer, new_payload), digest), digest
    )
    if updated == installer:
        print("already in sync")
        return 0
    print(f"out of sync; new payload sha256 {digest}")
    if args.write:
        INSTALLER.write_text(updated, encoding="utf-8")
        print(f"wrote {INSTALLER.relative_to(REPO)}")
    else:
        print("(dry run: pass --write to update)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
