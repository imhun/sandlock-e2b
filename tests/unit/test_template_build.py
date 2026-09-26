"""Template.build buildctl path (buildkit engine, no Docker daemon)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from control_plane.api import templates as tmpl
from control_plane.config import Settings
from control_plane.registry.templates import (
    TemplateRecord,
    TemplateRegistry,
    UnknownTemplateBuildError,
)


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


class _FakeTemplateRegistry:
    """Records save() calls: a mutated record must be persisted."""

    def __init__(self) -> None:
        self.saved: list = []
        self.discarded: list = []

    def save(self, record) -> None:  # noqa: ANN001
        self.saved.append(record)

    def save_build(self, record, build) -> None:  # noqa: ANN001
        # The real registry writes this to the shared volume so the *other*
        # replica can answer the status poll; the fake only has to exist.
        self.builds_saved = getattr(self, "builds_saved", [])
        self.builds_saved.append((record, build))

    def discard(self, record) -> None:  # noqa: ANN001
        self.discarded.append(record)


def _app(settings: Settings) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(settings=settings, templates=_FakeTemplateRegistry())
    )


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
) -> tuple[list, SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    captured: dict = {}

    async def fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    build = _build()
    template = _template()
    app = _app(settings)
    await tmpl._run_build(
        app, template, build, "FROM python:3.11-slim\n", workspace
    )
    return captured["args"], build, template, app


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
    args, build, template, app = await _run_build(monkeypatch, settings, tmp_path)

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
    # The record is re-read from disk on every lookup by name, so the switch
    # only holds if it was persisted; otherwise the next create resolves the
    # un-pushable e2b-local name again.
    assert app.state.templates.saved == [template]


@pytest.mark.asyncio
async def test_buildctl_builds_without_registry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(api_keys=("k",), image_registry="")
    args, build, template, app = await _run_build(monkeypatch, settings, tmp_path)

    output = args[args.index("--output") + 1]
    # Without a registry there is nothing to push to: buildkit exports an OCI
    # layout tar into the node image cache, and that tar is what the worker
    # resolves the template image from.
    assert output.startswith("type=oci,dest=")
    tar = Path(output.removeprefix("type=oci,dest="))
    assert tar.parent == settings.image_cache_dir / "_oci"
    assert tar.name == "e2b-sandlock-template_tpl_abc.oci.tar"
    assert build.status == "ready"
    assert template.image == "e2b-sandlock-template:tpl_abc"
    assert app.state.templates.saved == []


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


@pytest.mark.asyncio
async def test_a_failed_build_removes_its_half_written_oci_tar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """N19: buildctl opens ``dest`` before it knows the build will succeed.

    A failure therefore leaves a 0-byte (or truncated) layout tar behind, and
    that file is not just clutter: it is what a later name resolution can hand a
    worker, whose error (`Code.INTERNAL: … empty file`) says nothing about the
    real cause. The record goes with it -- see the registry tests -- because a
    name must only resolve to a build that produced an image.
    """
    settings = Settings(api_keys=("k",), image_registry="", image_oci_dir=tmp_path / "oci")
    tar = tmp_path / "oci" / "_oci" / "e2b-sandlock-template_tpl_abc.oci.tar"

    async def fake_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        # What buildctl leaves when the build fails: the output file exists.
        Path(args[args.index("--output") + 1].removeprefix("type=oci,dest=")).write_bytes(b"")
        return _FakeProc(code=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    build, template, app = _build(), _template(), _app(settings)
    await tmpl._run_build(app, template, build, "FROM python:3.11-slim\n", tmp_path)

    assert build.status == "error"
    assert not tar.exists(), "a half-written layout tar must not survive the failure"
    assert app.state.templates.discarded == [template]


def test_discarding_a_failed_build_frees_its_name(workspace: Path) -> None:
    """A name that resolves has to mean "a build produced an image".

    ``create`` publishes the record and binds the name before anything is built,
    so a failed build used to leave a name pointing at an image whose OCI tar
    buildkit never finished. The next build of that name could pick the debris
    up (F14 on the k0s cluster) and the worker reported `… empty file`.

    Discarding is deliberately *not* "delete everything": the build status stays
    readable by id, because that is what the SDK polls to learn why its build
    failed. Only the name and the on-disk record go.
    """
    registry = TemplateRegistry(workspace / "templates")
    record, build = registry.create("smoke-template")
    build.status = "error"
    build.error = "buildkit build exited with code 1"
    registry.discard(record)

    with pytest.raises(UnknownTemplateBuildError):
        registry.get_by_name("smoke-template")
    # The status poll addresses the build by id, not by name.
    assert registry.get(record.template_id).get_build(build.build_id).status == "error"
    # A template nobody can create from must not be advertised either.
    assert registry.list() == []
    # Nothing on disk for a later scan (or a restarted control plane) to find.
    assert not (workspace / "templates" / record.template_id).exists()
    restarted = TemplateRegistry(workspace / "templates")
    assert restarted.list() == []
    with pytest.raises(UnknownTemplateBuildError):
        restarted.get_by_name("smoke-template")


def test_a_failed_rebuild_leaves_the_previous_successful_build_resolvable(
    workspace: Path,
) -> None:
    """The name falls back to the last build that produced an image."""
    registry = TemplateRegistry(workspace / "templates")
    good, _ = registry.create("foo")
    failed, _ = registry.create("foo")
    assert registry.get_by_name("foo").template_id == failed.template_id

    registry.discard(failed)
    assert registry.get_by_name("foo").template_id == good.template_id


def test_name_resolution_picks_the_newest_record_not_the_last_one_scanned(
    workspace: Path,
) -> None:
    """Several records can carry one name, so the winner must be deterministic.

    Calling that a "rule" fixes the half of N19 that a fix alone cannot: clusters
    already carrying such debris keep the records, because they predate it. So
    the choice has to be *which* record, and ``created_at`` (persisted) is the
    honest answer -- the alternative was whichever one the directory walk
    happened to see last, which is how a failed build's record took a name back
    from the build that had replaced it.
    """
    base = workspace / "templates"
    registry = TemplateRegistry(base)
    older, _ = registry.create("foo")
    newer, _ = registry.create("foo")
    older.created_at = newer.created_at - 60
    registry.save(older)
    # Persisting the older record must not steal the binding back...
    assert registry.get_by_name("foo").template_id == newer.template_id

    # ...and a fresh process (the control plane restarting) only has the disk.
    restarted = TemplateRegistry(base)
    assert restarted.get_by_name("foo").template_id == newer.template_id


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


def test_a_build_started_on_one_replica_is_readable_on_the_other(workspace):
    """Two replicas share the volume, not their memory (F11 follow-up).

    ``Template.build`` is three requests: create the template, trigger the
    build, then poll its status. Nothing keeps them on one replica -- the SDK
    reaches whichever pod the Service picks -- and the build runs as an in-process
    task on the replica that took the trigger. The template record is persisted,
    but ``to_storage_dict`` deliberately leaves ``builds`` out, so the poll used
    to answer ``404 Template build … not found`` for a build that was running
    fine one replica over. Measured on k0s 2026-09-26: create landed on one pod,
    the trigger on the other, and the smoke's ``Template.build`` got
    ``404: Template build bld_2299e0da0ae79d1a not found``.

    The build therefore lives on the shared volume too: one small file per build,
    written as the state moves and as log lines arrive.
    """
    base = workspace / "templates"
    replica_a = TemplateRegistry(base)
    record, build = replica_a.create("smoke-template")
    # Exactly what the trigger does on the replica that received it.
    build.status = "building"
    build.append_log("building template (buildkit: unix:///run/buildkit)")
    replica_a.save_build(record, build)

    # The poll lands on the other replica, which has only the shared volume.
    replica_b = TemplateRegistry(base)
    seen = replica_b.get_build(record.template_id, build.build_id)
    assert seen.status == "building"
    assert seen.logs == ["building template (buildkit: unix:///run/buildkit)"]
    assert seen.error is None


def test_a_finished_build_reports_its_final_state_to_the_other_replica(workspace):
    base = workspace / "templates"
    replica_a = TemplateRegistry(base)
    record, build = replica_a.create("smoke-template")
    build.status = "ready"
    build.append_log("Build finished successfully")
    replica_a.save_build(record, build)

    info = TemplateRegistry(base).get_build(
        record.template_id, build.build_id
    ).as_info(record.template_id)
    assert info["status"] == "ready"
    assert info["logs"] == ["Build finished successfully"]
    assert info["buildID"] == build.build_id
    assert info["templateID"] == record.template_id


def test_a_build_file_is_written_where_the_other_replica_looks(workspace):
    """The layout is part of the contract: no shared registry, just a path."""
    base = workspace / "templates"
    registry = TemplateRegistry(base)
    record, build = registry.create("smoke-template")
    registry.save_build(record, build)

    path = base / record.template_id / "builds" / f"{build.build_id}.json"
    assert path.is_file(), path
    assert json.loads(path.read_text())["build_id"] == build.build_id
