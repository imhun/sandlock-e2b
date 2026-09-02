"""E6.2 upgrade.sh base-image digest validation.

Production deploys must pin ``E2B_BASE_IMAGE`` with a well-formed
``@sha256:`` digest; tag-only references fail closed unless
``--allow-tag-base-image`` is passed explicitly.
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


def _env_file(tmp_path: Path, value: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(f"E2B_BASE_IMAGE={value}\n", encoding="utf-8")
    return path


def test_valid_digest_passes(tmp_path: Path) -> None:
    ref = f"python:3.11-slim@sha256:{'a' * 64}"
    env_file = _env_file(tmp_path, ref)
    result = _bash(f'validate_env_file_base_image "{env_file}"')
    assert result.returncode == 0, result.stderr


def test_tag_only_ref_fails_without_flag(tmp_path: Path) -> None:
    env_file = _env_file(tmp_path, "python:3.11-slim")
    result = _bash(f'validate_env_file_base_image "{env_file}"')
    assert result.returncode == 1
    assert "必须固定 @sha256: digest" in result.stderr


def test_tag_only_ref_passes_with_explicit_allow_flag(tmp_path: Path) -> None:
    env_file = _env_file(tmp_path, "python:3.11-slim")
    result = _bash(
        f'ALLOW_TAG_BASE_IMAGE=1 validate_env_file_base_image "{env_file}"'
    )
    assert result.returncode == 0, result.stderr


def test_unresolved_placeholder_digest_fails(tmp_path: Path) -> None:
    env_file = _env_file(
        tmp_path,
        "registry.example.com/ns/python-mcp:3.14@sha256:__E2B_BASE_IMAGE_DIGEST__",
    )
    result = _bash(f'validate_env_file_base_image "{env_file}"')
    assert result.returncode == 1
    assert "未解析占位符" in result.stderr


def test_malformed_digest_fails(tmp_path: Path) -> None:
    for bad in (
        f"python:3.11-slim@sha256:{'a' * 63}",  # too short
        "python:3.11-slim@sha256:" + "G" * 64,  # non-hex
        "python:3.11-slim@sha256:" + "A" * 64,  # uppercase hex
    ):
        env_file = _env_file(tmp_path, bad)
        result = _bash(f'validate_env_file_base_image "{env_file}"')
        assert result.returncode == 1, bad
        assert "digest 格式非法" in result.stderr, bad
    # A non-sha256 algorithm is refused as well (only @sha256: is accepted).
    env_file = _env_file(tmp_path, "python:3.11-slim@sha512:" + "a" * 64)
    result = _bash(f'validate_env_file_base_image "{env_file}"')
    assert result.returncode == 1
    assert "必须固定 @sha256: digest" in result.stderr


def test_parse_image_ref_splits_tag_and_digest() -> None:
    result = _bash(
        'parse_image_ref "registry.example.com/ns/python-mcp:3.14@sha256:'
        f"{'b' * 64}\""
    )
    assert result.returncode == 0
    repo, tag, digest = result.stdout.strip().split("|")
    assert repo == "registry.example.com/ns/python-mcp"
    assert tag == "3.14"
    assert digest == f"@sha256:{'b' * 64}"

    result = _bash('parse_image_ref "python:3.11-slim"')
    repo, tag, digest = result.stdout.strip().split("|")
    assert repo == "python"
    assert tag == "3.11-slim"
    assert digest == ""


def test_env_examples_are_digest_pinned(tmp_path: Path) -> None:
    """Both shipped .env.example files carry the @sha256: form (E6.2)."""
    stack = (REPO / "deploy" / "stack" / ".env.example").read_text(encoding="utf-8")
    compose = (REPO / "deploy" / "compose" / ".env.example").read_text(
        encoding="utf-8"
    )
    stack_ref = next(
        line.split("=", 1)[1]
        for line in stack.splitlines()
        if line.startswith("E2B_BASE_IMAGE=")
    )
    compose_ref = next(
        line.split("=", 1)[1]
        for line in compose.splitlines()
        if line.startswith("E2B_BASE_IMAGE=")
    )
    assert "@sha256:" in stack_ref
    assert "@sha256:" in compose_ref
    # The compose example is directly deployable: it carries a real digest.
    compose_env = _env_file(tmp_path, compose_ref)
    result = _bash(f'validate_env_file_base_image "{compose_env}"')
    assert result.returncode == 0, result.stderr
    # The stack example uses an explicit placeholder that upgrade.sh refuses
    # until the operator resolves and pins the real digest.
    stack_env = _env_file(tmp_path, stack_ref)
    result = _bash(f'validate_env_file_base_image "{stack_env}"')
    assert result.returncode == 1
    assert "未解析占位符" in result.stderr
