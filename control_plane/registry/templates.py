"""Local template builds: Dockerfile -> image via the host Docker daemon."""

from __future__ import annotations

import threading
import time
import secrets
from dataclasses import dataclass, field
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

    def get_build(self, build_id: str) -> BuildRecord:
        build = self.builds.get(build_id)
        if build is None:
            raise UnknownTemplateBuildError(build_id)
        return build

    def is_file_uploaded(self, file_hash: str) -> bool:
        return self.files.get(file_hash, False)

    def mark_file_uploaded(self, file_hash: str) -> None:
        self.files[file_hash] = True

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
    def __init__(self) -> None:
        self._templates: dict[str, TemplateRecord] = {}
        self._by_name: dict[str, str] = {}
        self._lock = threading.Lock()

    def create(self, name: str) -> tuple[TemplateRecord, BuildRecord]:
        with self._lock:
            template_id = sandbox_id().replace("sbx_", "tpl_")
            build_id = sandbox_id().replace("sbx_", "bld_")
            record = TemplateRecord(
                template_id=template_id,
                name=name,
                image=f"e2b-local/{template_id}",
            )
            build = BuildRecord(build_id=build_id)
            record.builds[build_id] = build
            self._templates[template_id] = record
            self._by_name[name] = template_id
            return record, build

    def get(self, template_id: str) -> TemplateRecord:
        with self._lock:
            record = self._templates.get(template_id)
        if record is None:
            raise UnknownTemplateBuildError(template_id)
        return record

    def get_by_name(self, name: str) -> TemplateRecord:
        with self._lock:
            template_id = self._by_name.get(name)
        if template_id is None:
            raise UnknownTemplateBuildError(name)
        return self.get(template_id)

    def list(self) -> list[TemplateRecord]:
        with self._lock:
            return list(self._templates.values())
