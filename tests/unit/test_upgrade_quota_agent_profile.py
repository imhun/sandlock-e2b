"""A6/fix-1: stack-local quota-agent wiring in the deploy scripts.

``upgrade.sh --with-quota-agent`` must make the agent reachable on the target:
the ``QUOTA_AGENT_IMAGE`` tag is pinned (built+pushed by ``build-images.sh``),
``QUOTA_AGENT_PROFILE=1`` is sticky in ``.env`` so later upgrades keep
``--profile quota``, and the worker is pointed at the in-stack service. The
decision helpers live in ``deploy/scripts/lib/helpers.sh`` so they can be
exercised without touching a target host (``upgrade.sh`` itself SSHes into the
production box, so it is never executed by this suite).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
HELPERS = REPO / "deploy" / "scripts" / "lib" / "helpers.sh"


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


# --- env file primitives ----------------------------------------------------


def test_env_file_value_reads_the_last_assignment(tmp_path: Path) -> None:
    env_file = _env_file(
        tmp_path, "E2B_QUOTA_AGENT_TOKEN=first\nE2B_QUOTA_AGENT_TOKEN=second\n"
    )
    result = _bash(f'env_file_value "{env_file}" E2B_QUOTA_AGENT_TOKEN')
    assert result.returncode == 0
    assert result.stdout == "second"


def test_set_env_file_value_replaces_without_duplicating(tmp_path: Path) -> None:
    env_file = _env_file(tmp_path, "WORKER_IMAGE=old\nOTHER=keep\n")
    result = _bash(f'set_env_file_value "{env_file}" WORKER_IMAGE new')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == "WORKER_IMAGE=new\nOTHER=keep\n"


def test_set_env_file_value_appends_a_missing_key(tmp_path: Path) -> None:
    env_file = _env_file(tmp_path, "WORKER_IMAGE=old\n")
    result = _bash(f'set_env_file_value "{env_file}" QUOTA_AGENT_IMAGE img:1')
    assert result.returncode == 0, result.stderr
    assert (
        env_file.read_text(encoding="utf-8")
        == "WORKER_IMAGE=old\nQUOTA_AGENT_IMAGE=img:1\n"
    )


# --- profile decision (the gate upgrade.sh actually applies) -----------------


def test_quota_agent_profile_args_empty_when_profile_is_off(tmp_path: Path) -> None:
    bodies = (
        "E2B_QUOTA_AGENT_TOKEN=tok\n",
        "QUOTA_AGENT_PROFILE=0\nE2B_QUOTA_AGENT_TOKEN=tok\n",
    )
    for body in bodies:
        env_file = _env_file(tmp_path, body)
        result = _bash(f'quota_agent_profile_args "{env_file}"')
        assert result.returncode == 0
        assert result.stdout == ""
        assert result.stderr == ""


def test_quota_agent_profile_args_enabled_with_token(tmp_path: Path) -> None:
    env_file = _env_file(
        tmp_path, "QUOTA_AGENT_PROFILE=1\nE2B_QUOTA_AGENT_TOKEN=tok\n"
    )
    result = _bash(f'quota_agent_profile_args "{env_file}"')
    assert result.returncode == 0
    assert result.stdout == "--profile quota"
    assert result.stderr == ""


def test_quota_agent_profile_args_fails_closed_without_token(tmp_path: Path) -> None:
    env_file = _env_file(
        tmp_path, "QUOTA_AGENT_PROFILE=1\nE2B_QUOTA_AGENT_TOKEN=\n"
    )
    result = _bash(f'quota_agent_profile_args "{env_file}"')
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"QUOTA_AGENT_PROFILE=1 但 {env_file} 缺 E2B_QUOTA_AGENT_TOKEN："
        " quota-agent 拒绝无 auth 启动（deploy/quota_agent/__main__.py）\n"
    )


def test_quota_agent_profile_args_ignores_a_missing_env_file() -> None:
    result = _bash('quota_agent_profile_args "/nonexistent/.env"')
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


# --- --with-quota-agent effect -----------------------------------------------


def test_enable_quota_agent_profile_sets_switch_and_stack_url(tmp_path: Path) -> None:
    env_file = _env_file(
        tmp_path, "E2B_QUOTA_AGENT_URL=\nE2B_QUOTA_AGENT_TOKEN=tok\n"
    )
    result = _bash(f'enable_quota_agent_profile "{env_file}"')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == (
        "E2B_QUOTA_AGENT_URL=http://quota-agent:49984\n"
        "E2B_QUOTA_AGENT_TOKEN=tok\n"
        "QUOTA_AGENT_PROFILE=1\n"
    )


def test_enable_quota_agent_profile_keeps_an_external_url(tmp_path: Path) -> None:
    env_file = _env_file(
        tmp_path,
        "E2B_QUOTA_AGENT_URL=http://nfs-server:49984\nE2B_QUOTA_AGENT_TOKEN=tok\n",
    )
    result = _bash(f'enable_quota_agent_profile "{env_file}"')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == (
        "E2B_QUOTA_AGENT_URL=http://nfs-server:49984\n"
        "E2B_QUOTA_AGENT_TOKEN=tok\n"
        "QUOTA_AGENT_PROFILE=1\n"
    )
