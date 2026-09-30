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
UPGRADE = (REPO / "deploy" / "scripts" / "upgrade.sh").read_text(encoding="utf-8")


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
        " quota-agent 拒绝无 auth 启动（quota_agent/__main__.py）\n"
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


# --- --without-quota-agent effect (fix round 2) ------------------------------


def test_disable_quota_agent_profile_clears_the_stack_url(tmp_path: Path) -> None:
    """Off must stop the worker from reaching a leftover stack agent: compose
    keeps running a container whose service left the active profile set, so the
    switch alone would be a false "quota is off" statement."""
    env_file = _env_file(
        tmp_path,
        "QUOTA_AGENT_PROFILE=1\n"
        "E2B_QUOTA_AGENT_URL=http://quota-agent:49984\n"
        "E2B_QUOTA_AGENT_TOKEN=tok\n",
    )
    result = _bash(f'disable_quota_agent_profile "{env_file}"')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == (
        "QUOTA_AGENT_PROFILE=0\n"
        "E2B_QUOTA_AGENT_URL=\n"
        "E2B_QUOTA_AGENT_TOKEN=tok\n"
    )
    off = _bash(f'quota_agent_profile_args "{env_file}"')
    assert off.returncode == 0
    assert off.stdout == ""


def test_disable_quota_agent_profile_keeps_an_external_url(tmp_path: Path) -> None:
    """An operator-supplied external/NFS-side agent is not ours to disable; the
    switch goes off (so the stack container is removed) but the URL stays."""
    env_file = _env_file(
        tmp_path,
        "QUOTA_AGENT_PROFILE=1\n"
        "E2B_QUOTA_AGENT_URL=http://nfs-server:49984\n"
        "E2B_QUOTA_AGENT_TOKEN=tok\n",
    )
    result = _bash(f'disable_quota_agent_profile "{env_file}"')
    assert result.returncode == 0, result.stderr
    assert env_file.read_text(encoding="utf-8") == (
        "QUOTA_AGENT_PROFILE=0\n"
        "E2B_QUOTA_AGENT_URL=http://nfs-server:49984\n"
        "E2B_QUOTA_AGENT_TOKEN=tok\n"
    )


def test_off_path_removes_the_container_explicitly(tmp_path: Path) -> None:
    """The off path must not rely on --remove-orphans (compose 5.1.2 keeps the
    container), and the removal has to run before the pull/up."""
    rm_cmd = (
        "docker compose -f docker-compose.prod.yml --profile quota rm -sf quota-agent"
    )
    assert rm_cmd in UPGRADE
    stop_idx = UPGRADE.index('if [ "$QUOTA_AGENT_STOP" = "1" ]; then')
    assert stop_idx < UPGRADE.index(rm_cmd) < UPGRADE.index("pull --quiet")


def test_keep_image_tags_with_quota_agent_fails_closed(tmp_path: Path) -> None:
    """--keep-image-tags skips the pinning loop, so the guard must refuse to
    deploy the unpullable `<name>:latest` fallback."""
    env_file = _env_file(
        tmp_path,
        "QUOTA_AGENT_PROFILE=1\nE2B_QUOTA_AGENT_TOKEN=tok\nQUOTA_AGENT_IMAGE=\n",
    )
    result = _bash(f'require_pinned_quota_agent_image "{env_file}"')
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"quota-agent 形态已启用，但 {env_file} 的 QUOTA_AGENT_IMAGE 为空："
        " compose 会回落到 e2b-sandlock-quota-agent:latest，目标机拉不到。"
        " --keep-image-tags 会跳过镜像 tag 固定（就是这个组合）；"
        " 请显式写 QUOTA_AGENT_IMAGE=<registry>/<ns>/e2b-sandlock-quota-agent:<VERSION>，"
        " 或去掉 --keep-image-tags 让 upgrade.sh 自动固定。\n"
    )
    assert 'require_pinned_quota_agent_image "${ENV_FILE:-}" || exit 1' in UPGRADE


def test_require_pinned_quota_agent_image_accepts_a_pinned_tag(tmp_path: Path) -> None:
    env_file = _env_file(
        tmp_path, "QUOTA_AGENT_IMAGE=registry.example.com/ns/e2b-sandlock-quota-agent:1.0\n"
    )
    result = _bash(f'require_pinned_quota_agent_image "{env_file}"')
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_remote_secret_carry_over_includes_the_agent_token() -> None:
    """A blank local token must not clobber the deployed one.

    Two credentials, two reasons: `E2B_QUOTA_AGENT_TOKEN` (the quota agent
    refuses to start without auth, so the next upgrade would fail) and
    `E2B_C3_AGENT_TOKEN` (C3 Task 4 slice B -- the CP→agent channel's only
    credential; a redeploy that blanked it would 401 every file operation and
    every slot grant).
    """
    result = _bash('printf "%s" "$PRESERVED_REMOTE_SECRET_KEYS"')
    assert result.returncode == 0
    assert result.stdout == (
        "E2B_API_KEYS E2B_INTERNAL_API_KEY E2B_INTERNAL_API_KEYS "
        "E2B_IMAGE_REGISTRY_PASSWORD E2B_REDIS_PASSWORD E2B_SECRET_MASTER_KEY "
        "E2B_SECRET_MASTER_KEYS E2B_QUOTA_AGENT_TOKEN E2B_C3_AGENT_TOKEN"
    )
    assert "for key in $PRESERVED_REMOTE_SECRET_KEYS; do" in UPGRADE
