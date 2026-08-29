"""Template COPY build-context upload contract (no Docker required)."""

from __future__ import annotations

import asyncio
import io
import tarfile

import httpx


def _tar_bytes(filename: str = "requirements.txt", content: str = "requests==2.32.0\n") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = content.encode()
        info = tarfile.TarInfo(filename)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def _create_template(client) -> dict:
    resp = await client.post(
        "/v3/templates",
        headers={"X-API-Key": "local-key"},
        json={"name": "copy-contract"},
    )
    assert resp.status_code == 202
    return resp.json()


async def test_file_upload_link_then_present(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        file_hash = "abc123"

        link = await client.get(
            f"/templates/{template_id}/files/{file_hash}",
            headers={"X-API-Key": "local-key"},
        )
        assert link.status_code == 201
        assert link.json()["present"] is False
        url = link.json()["url"]
        assert url.startswith("http://test/templates/")
        assert "token=" in url

        upload = await client.put(url, content=_tar_bytes())
        assert upload.status_code == 204

        cached = await client.get(
            f"/templates/{template_id}/files/{file_hash}",
            headers={"X-API-Key": "local-key"},
        )
        assert cached.status_code == 201
        assert cached.json() == {"present": True, "url": None}


async def test_file_upload_rejects_bad_token(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        upload = await client.put(
            f"/templates/{template_id}/files/hash1/upload?token=wrong",
            content=_tar_bytes(),
        )
        assert upload.status_code == 401


async def test_file_upload_rejects_invalid_archive(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        link = await client.get(
            f"/templates/{template_id}/files/hash2",
            headers={"X-API-Key": "local-key"},
        )
        url = link.json()["url"]
        upload = await client.put(url, content=b"not a tar archive")
        assert upload.status_code == 400


async def test_copy_step_build_errors_without_context(apps):
    """A COPY step with no uploaded files must fail with a clear message."""
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        build_id = info["buildID"]
        resp = await client.post(
            f"/v2/templates/{template_id}/builds/{build_id}",
            headers={"X-API-Key": "local-key"},
            json={
                "fromImage": "python:3.11-slim",
                "steps": [
                    {
                        "type": "COPY",
                        "args": ["requirements.txt", "/app/", "", ""],
                    }
                ],
            },
        )
        assert resp.status_code == 202
        status = None
        for _ in range(50):
            status = await client.get(
                f"/templates/{template_id}/builds/{build_id}/status",
                headers={"X-API-Key": "local-key"},
            )
            if status.json()["status"] in ("error", "ready"):
                break
            await asyncio.sleep(0.1)
        assert status.status_code == 200
        # No upload was made, so the build context lacks the file and the
        # Docker daemon (even when present) cannot resolve the COPY source.
        assert status.json()["status"] == "error"
