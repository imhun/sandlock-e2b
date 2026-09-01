"""Template.build buildctl path (buildkit engine, no Docker daemon)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane.api import templates as tmpl
from control_plane.config import Settings
from control_plane.registry.templates import TemplateRecord, TemplateRegistry


class _FakeStream:
    async def readline(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, code: int = 0) -> None:
        self._code = code

    @property
    def stdout(self) -> _FakeStream:
        return _FakeStream()

    async def wait(self) -> int:
        return self._code


def _app(settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(state=SimpleNamespace(settings=settings))


def _template() -> SimpleNamespace:
    return SimpleNamespace(
        template_id="tpl_abc",
        image="e2b-sandlock-template:tpl_abc",
    )


def _build() -> SimpleNamespace:
    logs: list[str] = []
    return SimpleNamespace(status="pending", error="", append_log=logs.append)


async def _run_build(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    workspace: Path,
) -> tuple[list, SimpleNamespace, SimpleNamespace]:
    captured: dict = {}

    async def fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    build = _build()
    template = _template()
    await tmpl._run_build(
        _app(settings), template, build, "FROM python:3.11-slim\n", workspace
    )
    return captured["args"], build, template


@pytest.mark.asyncio
async def test_buildctl_builds_and_pushes_to_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        api_keys=("k",),
        image_registry="registry.example.com/e2b",
        image_registry_username="user",
        image_registry_password="pass",
    )
    args, build, template = await _run_build(monkeypatch, settings, tmp_path)

    assert args[0] == "buildctl"
    assert "--addr" in args
    assert "--frontend" in args
    assert "dockerfile.v0" in args
    assert "--local" in args
    assert any(a.startswith("context=") for a in args)
    assert any(a.startswith("dockerfile=") for a in args)
    output = args[args.index("--output") + 1]
    assert (
        output
        == "type=image,name=registry.example.com/e2b/tpl_abc:latest,push=true"
    )
    assert build.status == "ready"
    assert template.image == "registry.example.com/e2b/tpl_abc"


@pytest.mark.asyncio
async def test_buildctl_builds_without_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(api_keys=("k",), image_registry="")
    args, build, template = await _run_build(monkeypatch, settings, tmp_path)

    output = args[args.index("--output") + 1]
    assert output == "type=image,name=e2b-sandlock-template:tpl_abc"
    assert build.status == "ready"
    # No registry: the template keeps its local image name.
    assert template.image == "e2b-sandlock-template:tpl_abc"


def test_write_docker_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """buildctl resolves push credentials from ~/.docker/config.json."""
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    settings = Settings(
        api_keys=("k",),
        image_registry="registry.cn-shanghai.aliyuncs.com/byteplan",
        image_registry_username="user",
        image_registry_password="pass",
    )
    tmpl._write_docker_config(settings)

    import base64

    config = json.loads(
        (tmp_path / ".docker" / "config.json").read_text(encoding="utf-8")
    )
    auth = config["auths"]["registry.cn-shanghai.aliyuncs.com"]["auth"]
    assert base64.b64decode(auth).decode() == "user:pass"


def test_write_docker_config_skips_without_creds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    settings = Settings(api_keys=("k",), image_registry="reg.example.com/e2b")
    tmpl._write_docker_config(settings)
    assert not (tmp_path / ".docker" / "config.json").exists()


def test_mark_file_uploaded_clears_token_idempotently():
    record = TemplateRecord(template_id="tpl_x", name="x", image="img")
    token = record.upload_url_token("h1")
    assert record.verify_upload_token("h1", token) is True

    record.mark_file_uploaded("h1")
    assert record.is_file_uploaded("h1") is True
    assert "h1" not in record.upload_tokens
    assert record.verify_upload_token("h1", token) is False

    record.mark_file_uploaded("h1")
    assert record.is_file_uploaded("h1") is True
    assert "h1" not in record.upload_tokens


def test_claim_file_upload_is_atomic_and_persists(workspace):
    registry = TemplateRegistry(workspace / "templates")
    record, _ = registry.create("x")
    record.upload_url_token("h1")

    assert registry.claim_file_upload(record.template_id, "h1") is True
    assert registry.claim_file_upload(record.template_id, "h1") is False

    loaded = registry.get(record.template_id)
    assert loaded.is_file_uploaded("h1") is True
    assert "h1" not in loaded.upload_tokens
    # Persisted on disk: a fresh registry sees the uploaded state.
    restarted = TemplateRegistry(workspace / "templates")
    assert restarted.get(record.template_id).is_file_uploaded("h1") is True
