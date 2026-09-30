"""Task 4 slice B: the stack's `.env` must end up with `E2B_C3_AGENT_TOKEN`.

`deploy/stack/docker-compose.prod.yml` gives its control plane and both agent
faces no default for that credential (deliberately: a missing credential has to
fail loudly), and `c3_agent/__main__.py` exits without one. But the key
only arrived in slice B, so every `.env` written before it lacks the line --
including the one `upgrade.sh` pulls off a target host on the next deploy. The
carry-over loop there only rewrites keys that are *present* (blank or holding a
``__PLACEHOLDER__``), so a missing key would sail through and leave both agent
faces exiting until an operator added it by hand.

The fix is the ``ensure_c3_agent_token`` helper in
``deploy/scripts/lib/helpers.sh`` (add-if-missing, preserve a deployed value)
called from ``upgrade.sh`` right after the carry-over loop. These tests drive the
helper against a real ``.env`` file and pin the call site's order, rather than
matching on prose.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
HELPERS = REPO / "deploy" / "scripts" / "lib" / "helpers.sh"
UPGRADE = (REPO / "deploy" / "scripts" / "upgrade.sh").read_text(encoding="utf-8")

#: What `openssl rand -hex 24` produces -- the helper's generated shape.
_GENERATED = re.compile(r"\A[0-9a-f]{48}\Z")


def _bash(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-lc", f". {HELPERS}; {script}"],
        capture_output=True,
        text=True,
    )


def _env_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_a_missing_token_is_generated(tmp_path: Path) -> None:
    """The slice-B arrival case: the line is simply not there."""
    env_file = _env_file(tmp_path, "WORKER_IMAGE=img:1\n")
    result = _bash(f'ensure_c3_agent_token "{env_file}" ""')
    assert result.returncode == 0, result.stderr
    lines = dict(
        line.split("=", 1) for line in env_file.read_text(encoding="utf-8").splitlines()
    )
    assert lines["WORKER_IMAGE"] == "img:1"
    assert _GENERATED.match(lines["E2B_C3_AGENT_TOKEN"]), lines


def test_the_template_placeholder_is_not_a_token(tmp_path: Path) -> None:
    """A template deployed without its `sed` must not keep the literal."""
    env_file = _env_file(tmp_path, "E2B_C3_AGENT_TOKEN=__C3_AGENT_TOKEN__\n")
    result = _bash(f'ensure_c3_agent_token "{env_file}" ""')
    assert result.returncode == 0, result.stderr
    value = env_file.read_text(encoding="utf-8").split("=", 1)[1].strip()
    assert _GENERATED.match(value), value


def test_an_empty_value_is_filled_and_a_real_one_is_kept(tmp_path: Path) -> None:
    """Empty is "no usable value"; a deployed token is never rewritten.

    The last part is what makes a redeploy safe: the two agent faces and the
    control plane authenticate each other with this value, so rewriting it on
    an unrelated upgrade would 401 every file operation and every slot grant.
    """
    empty = _env_file(tmp_path, "E2B_C3_AGENT_TOKEN=\n")
    result = _bash(f'ensure_c3_agent_token "{empty}" ""')
    assert result.returncode == 0, result.stderr
    filled = empty.read_text(encoding="utf-8").split("=", 1)[1].strip()
    assert _GENERATED.match(filled), filled

    kept = tmp_path / "kept.env"
    kept.write_text("E2B_C3_AGENT_TOKEN=already-there\n", encoding="utf-8")
    result = _bash(f'ensure_c3_agent_token "{kept}" "some-other-value"')
    assert result.returncode == 0, result.stderr
    assert kept.read_text(encoding="utf-8") == "E2B_C3_AGENT_TOKEN=already-there\n"


def test_the_deployed_remote_value_wins_over_generating_a_new_one(
    tmp_path: Path,
) -> None:
    """A target that already has one keeps it -- no fleet-wide re-auth."""
    env_file = _env_file(tmp_path, "WORKER_IMAGE=img:1\n")
    result = _bash(f'ensure_c3_agent_token "{env_file}" "deployed-value"')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        "WORKER_IMAGE=img:1",
        "E2B_C3_AGENT_TOKEN=deployed-value",
    ]


def test_a_missing_env_file_is_not_an_error(tmp_path: Path) -> None:
    """`--keep-image-tags`/no `.env` paths still have to reach compose."""
    missing = tmp_path / "nope.env"
    result = _bash(f'ensure_c3_agent_token "{missing}" ""')
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert not missing.exists()


def test_upgrade_backfills_the_token_before_it_pins_the_images() -> None:
    """The call site, by order: after carry-over, before the image pin loop.

    Order matters in both directions. Before carry-over, a value already
    deployed on the target would be invisible to this helper and a *new*
    credential would be generated (the fleet would then re-authenticate on a
    routine upgrade); after the image pin loop it would be too late for the
    same run's `docker compose pull/up`.
    """
    carry_over = UPGRADE.index("已从远端保留")
    backfill = UPGRADE.index('ensure_c3_agent_token "$ENV_FILE" "$C3_TOKEN_REMOTE"')
    pin_loop = UPGRADE.index("镜像 tag 固定为版本")
    assert carry_over < backfill < pin_loop
    # ...and it asks the target for the value it already has, so the deployed
    # credential survives a redeploy that never had it locally.
    assert (
        'C3_TOKEN_REMOTE="$(remote_env_value E2B_C3_AGENT_TOKEN || true)"' in UPGRADE
    )
