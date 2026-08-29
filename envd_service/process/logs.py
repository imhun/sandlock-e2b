"""Command output logging.

Each sandbox appends its command stdout/stderr/PTY output to
``command-logs.jsonl`` inside the workspace so the control plane can merge
command output into ``GET /sandboxes/{id}/logs`` (also survives snapshots and
filesystem migration).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from gateway_common.timeutil import to_iso_z, utcnow

logger = logging.getLogger(__name__)

# Same ANSI escape set the e2b SDK strips before surfacing command output.
_ANSI_RE = re.compile(
    r"(?:\u0007|\u001B\u005C|\u009B)|"
    r"[\u001B\u009B][\[\]()#;?]*(?:\d{1,4}(?:[;:]\d{0,4})*)?[\dA-PR-TZcf-nq-uy=><~]"
)

_MAX_COMMAND_CHARS = 1_000_000
_MAX_FILE_BYTES = 16 * 1024 * 1024
_TRUNCATED_MARK = "... output truncated ..."


def _clean(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    return text.replace("\r\n", "\n").replace("\r", "\n")


class CommandLogWriter:
    """Line-oriented JSONL appender for one sandbox workspace."""

    def __init__(self, workspace_dir: str | Path) -> None:
        self._path = Path(workspace_dir) / "command-logs.jsonl"
        self._pending: dict[int, str] = {}
        self._written: dict[int, int] = {}
        self._truncated: set[int] = set()
        self._file_bytes = self._path.stat().st_size if self._path.is_file() else 0
        self._file_full = False

    def start(self, pid: int, cmd: list[str]) -> None:
        self._emit(f"> {' '.join(cmd)}")
        self._written[pid] = 0
        self._truncated.discard(pid)

    def write(self, pid: int, stream: str, data: bytes) -> None:
        if self._file_full or pid in self._truncated:
            return
        text = data.decode("utf-8", "replace")
        buf = self._pending.get(pid, "") + text
        if "\n" not in buf:
            self._pending[pid] = buf
            return
        lines = buf.split("\n")
        self._pending[pid] = lines.pop()
        for line in lines:
            line = _clean(line).rstrip("\r")
            if not line:
                continue
            if stream == "stderr":
                line = f"stderr: {line}"
            self._emit(line)
            self._written[pid] = self._written.get(pid, 0) + len(line) + 1
            if self._written[pid] > _MAX_COMMAND_CHARS:
                self._emit(_TRUNCATED_MARK)
                self._truncated.add(pid)
                return

    def end(self, pid: int, exit_code: int | None) -> None:
        pending = self._pending.pop(pid, "")
        if pending and pid not in self._truncated:
            line = _clean(pending).rstrip("\r")
            if line:
                self._emit(line)
        self._written.pop(pid, None)
        self._truncated.discard(pid)
        if exit_code is not None:
            self._emit(f"exit: {exit_code}")

    def _emit(self, line: str) -> None:
        if self._file_full:
            return
        entry = {"timestamp": to_iso_z(utcnow()), "line": line}
        raw = (json.dumps(entry, separators=(",", ":")) + "\n").encode()
        if self._file_bytes + len(raw) > _MAX_FILE_BYTES:
            self._file_full = True
            mark = {
                "timestamp": to_iso_z(utcnow()),
                "line": "... command log size limit reached ...",
            }
            raw = (json.dumps(mark, separators=(",", ":")) + "\n").encode()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "ab") as f:
                f.write(raw)
            self._file_bytes += len(raw)
        except OSError:
            self._file_full = True
