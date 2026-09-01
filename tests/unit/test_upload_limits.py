"""E4.2: streaming upload helpers enforce byte limits without buffering."""

from __future__ import annotations

import pytest

from gateway_common.upload import (
    UploadTooLargeError,
    check_content_length,
    limit_bytes_from_mb,
    stream_body_to_file,
    stream_upload_file_to_file,
)


class _FakeRequest:
    def __init__(self, chunks: list[bytes], headers: dict | None = None) -> None:
        self._chunks = list(chunks)
        self.headers = headers or {}

    def stream(self):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


class _FakeUploadFile:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, size: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


async def test_stream_body_writes_all_within_limit(tmp_path):
    request = _FakeRequest([b"x" * 100] * 4)
    target = tmp_path / "f.bin"
    assert await stream_body_to_file(request, target, 1000) == 400
    assert target.read_bytes() == b"x" * 400


async def test_stream_body_over_limit_raises_and_removes_partial(tmp_path):
    request = _FakeRequest([b"x" * 100] * 10)
    target = tmp_path / "f.bin"
    with pytest.raises(UploadTooLargeError) as exc:
        await stream_body_to_file(request, target, 500)
    assert exc.value.limit_bytes == 500
    assert not target.exists()


async def test_stream_body_unlimited_when_limit_none(tmp_path):
    request = _FakeRequest([b"x" * 100] * 10)
    target = tmp_path / "f.bin"
    assert await stream_body_to_file(request, target, None) == 1000
    assert target.read_bytes() == b"x" * 1000


async def test_content_length_precheck_rejects_without_reading(tmp_path):
    request = _FakeRequest([b"x" * 100], {"content-length": "600"})
    with pytest.raises(UploadTooLargeError):
        check_content_length(request, 500)

    check_content_length(_FakeRequest([b"x"], {"content-length": "500"}), 500)
    check_content_length(_FakeRequest([b"x"], {"content-length": "499"}), 500)
    # Missing or malformed Content-Length falls through to streaming checks.
    check_content_length(_FakeRequest([b"x"], {}), 500)
    check_content_length(_FakeRequest([b"x"], {"content-length": "abc"}), 500)
    # No limit = no precheck.
    check_content_length(_FakeRequest([b"x"], {"content-length": "999999"}), None)


async def test_stream_upload_file_bounded(tmp_path):
    target = tmp_path / "up.bin"
    assert (
        await stream_upload_file_to_file(
            _FakeUploadFile([b"z" * 64] * 4), target, 256
        )
        == 256
    )
    assert target.read_bytes() == b"z" * 256

    over = tmp_path / "over.bin"
    with pytest.raises(UploadTooLargeError):
        await stream_upload_file_to_file(
            _FakeUploadFile([b"z" * 64] * 10), over, 128
        )
    assert not over.exists()


def test_limit_bytes_from_mb():
    assert limit_bytes_from_mb(None) is None
    assert limit_bytes_from_mb(0) is None
    assert limit_bytes_from_mb(-1) is None
    assert limit_bytes_from_mb(1) == 1024 * 1024
    assert limit_bytes_from_mb(512) == 512 * 1024 * 1024
