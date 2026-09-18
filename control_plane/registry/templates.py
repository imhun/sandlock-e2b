"""Local template builds: Dockerfile -> image via the host Docker daemon."""

from __future__ import annotations

import json
import shutil
import threading
import time
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gateway_common.ids import sandbox_id
from gateway_common.timeutil import to_iso_z, utcnow


class UnknownTemplateBuildError(KeyError):
    pass


@dataclass
class BuildRecord:
    build_id: str
    status: str = "waiting"  # waiting | building | ready | error
    logs: list[str] = field(default_factory=list)
    log_entries: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    started_at: float = field(default_factory=time.time)

    def append_log(self, line: str) -> None:
        self.logs.append(line)
        self.log_entries.append(
            {
                "timestamp": to_iso_z(utcnow()),
                "message": line,
                "level": "info",
            }
        )

    def as_info(self, template_id: str) -> dict[str, Any]:
        info: dict[str, Any] = {
            "templateID": template_id,
            "buildID": self.build_id,
            "status": self.status,
            "logs": list(self.logs),
            "logEntries": list(self.log_entries),
        }
        if self.error:
            info["reason"] = {
                "message": self.error,
                "step": None,
                "logEntries": [],
            }
        return info


@dataclass
class TemplateRecord:
    template_id: str
    name: str
    image: str
    created_at: float = field(default_factory=time.time)
    builds: dict[str, BuildRecord] = field(default_factory=dict)
    # filesHash -> upload state for COPY build-context files.
    files: dict[str, bool] = field(default_factory=dict)
    upload_tokens: dict[str, str] = field(default_factory=dict)
    tenant_id: str | None = None
    #: Set by :meth:`TemplateRegistry.discard` for a record whose build never
    #: produced an image. Never persisted (see ``_write_record``): a discarded
    #: record stops being resolvable by name and stops being written, but stays
    #: in memory so ``GET …/builds/{id}/status`` can still explain the failure.
    discarded: bool = False

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "name": self.name,
            "image": self.image,
            "created_at": self.created_at,
            "files": dict(self.files),
            "upload_tokens": dict(self.upload_tokens),
            "tenant_id": self.tenant_id,
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, Any]) -> "TemplateRecord":
        return cls(
            template_id=data["template_id"],
            name=data["name"],
            image=data["image"],
            created_at=float(data.get("created_at", time.time())),
            files=dict(data.get("files", {})),
            upload_tokens=dict(data.get("upload_tokens", {})),
            tenant_id=data.get("tenant_id"),
        )

    def get_build(self, build_id: str) -> BuildRecord:
        build = self.builds.get(build_id)
        if build is None:
            raise UnknownTemplateBuildError(build_id)
        return build

    def is_file_uploaded(self, file_hash: str) -> bool:
        return self.files.get(file_hash, False)

    def mark_file_uploaded(self, file_hash: str) -> None:
        """Record an uploaded file and drop its upload token (E3.4).

        Idempotent: clearing a missing token is a no-op, so repeated calls
        leave the uploaded state intact. The token is gone once the file is
        uploaded, so a later PUT with the old URL can never overwrite it.
        """
        self.files[file_hash] = True
        self.upload_tokens.pop(file_hash, None)

    def upload_url_token(self, file_hash: str) -> str:
        """Return (and lazily create) the token guarding the upload URL."""
        token = self.upload_tokens.get(file_hash)
        if token is None:
            token = secrets.token_urlsafe(32)
            self.upload_tokens[file_hash] = token
        return token

    def verify_upload_token(self, file_hash: str, token: str) -> bool:
        expected = self.upload_tokens.get(file_hash)
        return expected is not None and secrets.compare_digest(expected, token)


class TemplateRegistry:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._templates: dict[str, TemplateRecord] = {}
        self._by_name: dict[str, str] = {}
        self._lock = threading.Lock()
        self._base = Path(base_dir).resolve() if base_dir is not None else None
        if self._base is not None:
            self._base.mkdir(parents=True, exist_ok=True)

    def create(
        self, name: str, tenant_id: str | None = None
    ) -> tuple[TemplateRecord, BuildRecord]:
        with self._lock:
            template_id = sandbox_id().replace("sbx_", "tpl_")
            build_id = sandbox_id().replace("sbx_", "bld_")
            record = TemplateRecord(
                template_id=template_id,
                name=name,
                image=f"e2b-local/{template_id}",
                tenant_id=tenant_id,
            )
            build = BuildRecord(build_id=build_id)
            record.builds[build_id] = build
            self._templates[template_id] = record
            self._bind_name(record)
            self._write_record(record)
            return record, build

    def _bind_name(self, record: TemplateRecord) -> None:
        """Point ``record.name`` at the newest record claiming it.

        Names are not unique: build ``foo`` twice and both records stay on disk.
        Binding whichever one a directory walk happened to reach last made the
        winner depend on iteration order, which is how a *failed* build's record
        could take a name back from the build that replaced it (N19 / F14 on the
        k0s cluster). ``created_at`` is persisted, so "newest wins" is both
        deterministic and durable.
        """
        current_id = self._by_name.get(record.name)
        if current_id is not None and current_id != record.template_id:
            current = self._templates.get(current_id)
            if current is not None and current.created_at >= record.created_at:
                return
        self._by_name[record.name] = record.template_id

    def _record_path(self, template_id: str) -> Path | None:
        if self._base is None:
            return None
        return self._base / template_id / "template.json"

    def _write_record(self, record: TemplateRecord) -> None:
        path = self._record_path(record.template_id)
        if path is None or record.discarded:
            # A discarded record must not come back to life: the whole point is
            # that a name only resolves to a build that produced an image.
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(record.to_storage_dict(), separators=(",", ":")),
            encoding="utf-8",
        )

    def _scan_disk(self) -> None:
        if self._base is None or not self._base.is_dir():
            return
        for entry in sorted(self._base.iterdir()):
            if not entry.is_dir():
                continue
            path = entry / "template.json"
            if not path.is_file():
                continue
            template_id = entry.name
            if template_id in self._templates:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = TemplateRecord.from_storage_dict(payload)
                self._templates[record.template_id] = record
                self._bind_name(record)
            except (OSError, ValueError, KeyError):
                continue

    def get(self, template_id: str) -> TemplateRecord:
        with self._lock:
            record = self._templates.get(template_id)
            if record is None:
                self._scan_disk()
                record = self._templates.get(template_id)
        if record is None:
            raise UnknownTemplateBuildError(template_id)
        return record

    def get_by_name(self, name: str) -> TemplateRecord:
        with self._lock:
            self._scan_disk()
            template_id = self._by_name.get(name)
        if template_id is None:
            raise UnknownTemplateBuildError(name)
        return self.get(template_id)

    def save(self, record: TemplateRecord) -> None:
        """Persist a mutated record (upload tokens / uploaded state)."""
        with self._lock:
            self._templates[record.template_id] = record
            self._bind_name(record)
        self._write_record(record)

    def discard(self, record: TemplateRecord) -> None:
        """Drop a template whose build produced no image (N19).

        ``create`` publishes the record -- and binds its name -- *before* anything
        is built, so a failed build used to leave a name that resolves: to an
        image whose OCI layout tar is a 0-byte file buildkit never finished. The
        next build of that name could then pick the debris up, and the worker's
        failure (`Code.INTERNAL: file could not be opened successfully: … empty
        file`) pointed at the wrong thing entirely.

        Only the name and the on-disk record go. The record stays in memory (and
        out of :meth:`list`) because ``GET …/builds/{id}/status`` is how the SDK
        learns *why* its build failed -- answering "template not found" there
        would trade one confusing error for another.

        The trade that buys: a control-plane restart in the seconds between the
        failure and the SDK's poll turns that answer into a 404. The alternative
        (persist the failed record so the poll survives) recreates exactly what
        this method exists to remove -- a durable record for a template that
        cannot be built from.
        """
        with self._lock:
            record.discarded = True
            if self._by_name.get(record.name) == record.template_id:
                self._by_name.pop(record.name, None)
            # Another record may still carry the name (a failed rebuild of a
            # template that built before): let the newest remaining one have it.
            for candidate in self._templates.values():
                if candidate is record or candidate.discarded:
                    continue
                if candidate.name == record.name:
                    self._bind_name(candidate)
            path = self._record_path(record.template_id)
        if path is not None:
            with suppress(OSError):
                shutil.rmtree(path.parent, ignore_errors=True)

    def claim_file_upload(self, template_id: str, file_hash: str) -> bool:
        """Atomically mark ``file_hash`` uploaded; False when already done.

        Runs under the registry lock so two concurrent PUTs carrying the
        same token cannot both pass the "already uploaded" check; the loser
        is rejected with 409 and must discard its archive.
        """
        with self._lock:
            record = self._templates.get(template_id)
            if record is None:
                raise UnknownTemplateBuildError(template_id)
            if record.is_file_uploaded(file_hash):
                return False
            record.files[file_hash] = True
            record.upload_tokens.pop(file_hash, None)
            self._write_record(record)
            return True

    def list(self, *, tenant_id: str | None = None) -> list[TemplateRecord]:
        with self._lock:
            self._scan_disk()
            # A discarded template is not a template this deployment can create
            # from, so it is not advertised.
            records = [r for r in self._templates.values() if not r.discarded]
        if tenant_id is not None:
            records = [r for r in records if r.tenant_id == tenant_id]
        return records
