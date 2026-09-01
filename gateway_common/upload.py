"""Streaming request-body helpers with byte limits (E4.2).

``await request.body()`` buffers the entire upload in memory before anything
is written to disk, so an oversized file is a memory DoS on the control plane
and worker. These helpers drain the request (or a multipart ``UploadFile``)
in chunks straight to disk and abort with :class:`UploadTooLargeError` as
soon as the configured byte limit is crossed.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any

from fastapi import Request


class UploadTooLargeError(Exception):
    """Raised when a streamed body exceeds the configured write limit."""

    def __init__(self, limit_bytes: int | None) -> None:
        super().__init__(f"upload exceeds the {limit_bytes}-byte limit")
        self.limit_bytes = limit_bytes


def limit_bytes_from_mb(mb: int | None) -> int | None:
    """Map a MiB setting to bytes; 0/None disables the limit (repo convention)."""
    if not mb or int(mb) <= 0:
        return None
    return int(mb) * 1024 * 1024


def declared_content_length(request: Request) -> int | None:
    raw = request.headers.get("content-length")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def check_content_length(request: Request, limit_bytes: int | None) -> None:
    """Reject a request whose declared Content-Length already exceeds the limit."""
    if limit_bytes is None:
        return
    length = declared_content_length(request)
    if length is not None and length > limit_bytes:
        raise UploadTooLargeError(limit_bytes)


async def read_json_body(request: Request, limit_bytes: int | None) -> Any:
    """Read a JSON request body, bounded by ``limit_bytes`` (E5.3).

    Over-limit bodies (declared or actual) raise
    :class:`UploadTooLargeError` (the caller maps it to 413); malformed JSON
    raises :class:`json.JSONDecodeError` (the caller maps it to 400).
    """
    if limit_bytes is not None:
        check_content_length(request, limit_bytes)
    raw = await request.body()
    if limit_bytes is not None and len(raw) > limit_bytes:
        raise UploadTooLargeError(limit_bytes)
    return json.loads(raw)


def json_size(value: Any) -> int:
    """Serialized UTF-8 byte length of a JSON value (compact separators)."""
    return len(json.dumps(value, separators=(",", ":")).encode("utf-8"))


async def stream_body_to_file(
    request: Request,
    path: str | Path,
    limit_bytes: int | None,
) -> int:
    """Drain ``request`` body to ``path`` in chunks, bounded by ``limit_bytes``.

    ``path`` is removed if the limit is crossed or the stream fails. Returns
    the number of bytes written.
    """

    async def chunks() -> AsyncIterable[bytes]:
        async for chunk in request.stream():
            if chunk:
                yield chunk

    return await _drain(chunks(), Path(path), limit_bytes)


async def stream_upload_file_to_file(
    file_obj,
    path: str | Path,
    limit_bytes: int | None,
    *,
    chunk_size: int = 64 * 1024,
) -> int:
    """Drain a multipart ``UploadFile`` to ``path`` in bounded chunks."""

    async def chunks() -> AsyncIterable[bytes]:
        while True:
            chunk = await file_obj.read(chunk_size)
            if not chunk:
                return
            yield chunk

    return await _drain(chunks(), Path(path), limit_bytes)


async def _drain(
    iterable: AsyncIterable[bytes], path: Path, limit_bytes: int | None
) -> int:
    total = 0
    try:
        with open(path, "wb") as f:
            async for chunk in iterable:
                total += len(chunk)
                if limit_bytes is not None and total > limit_bytes:
                    raise UploadTooLargeError(limit_bytes)
                f.write(chunk)
        return total
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
