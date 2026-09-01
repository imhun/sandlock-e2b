"""Template COPY build-context upload contract (no Docker required)."""

from __future__ import annotations

import asyncio
import io
import os
import tarfile
import threading

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


async def test_uploaded_file_token_cleared_and_overwrite_rejected(apps):
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        file_hash = "abc123"
        first = _tar_bytes("requirements.txt", "requests==2.32.0\n")
        second = _tar_bytes("requirements.txt", "torch==2.1.0\n")

        link = await client.get(
            f"/templates/{template_id}/files/{file_hash}",
            headers={"X-API-Key": "local-key"},
        )
        url = link.json()["url"]
        assert link.json()["present"] is False
        upload = await client.put(url, content=first)
        assert upload.status_code == 204

        # The token is cleared: a replayed PUT is rejected and cannot
        # overwrite the already-cached build context.
        replay = await client.put(url, content=second)
        assert replay.status_code == 409
        assert replay.json() == {"code": 409, "message": "File already uploaded"}

        record = control.state.templates.get(template_id)
        assert record.is_file_uploaded(file_hash) is True
        assert file_hash not in record.upload_tokens

        # A fresh link keeps returning "already present" with no new token.
        cached = await client.get(
            f"/templates/{template_id}/files/{file_hash}",
            headers={"X-API-Key": "local-key"},
        )
        assert cached.status_code == 201
        assert cached.json() == {"present": True, "url": None}
        assert file_hash not in control.state.templates.get(template_id).upload_tokens

        # The cached archive on disk is the first upload, untouched.
        import tarfile

        archive = (
            control.state.workspace_base
            / "_builds"
            / template_id
            / "archives"
            / f"{file_hash}.tar.gz"
        )
        with tarfile.open(archive, mode="r:gz") as tar:
            extracted = tar.extractfile("requirements.txt").read()
        assert extracted == b"requests==2.32.0\n"


async def test_concurrent_upload_same_file_hash_keeps_winner_archive(
    apps, monkeypatch
):
    """E3.4 review I2: two PUTs racing on one file_hash keep one archive.

    Both requests stage unique temp files and atomically rename them into
    place; the loser of claim_file_upload must not unlink the winner's
    archive. Exactly one PUT wins (204), the other is rejected (409), and
    the final archive still exists with the uploaded content.
    """
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        file_hash = "race-hash"
        link = await client.get(
            f"/templates/{template_id}/files/{file_hash}",
            headers={"X-API-Key": "local-key"},
        )
        assert link.status_code == 201
        assert link.json()["present"] is False
        url = link.json()["url"]
        payload = _tar_bytes("requirements.txt", "requests==2.32.0\n")

    # Force both PUTs to finish staging (os.replace) before either claims,
    # so a buggy loser cleanup would deterministically delete the archive
    # the winner just claimed.
    barrier = threading.Barrier(2)
    real_replace = os.replace

    def delayed_replace(src, dst):
        barrier.wait(timeout=5)
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", delayed_replace)

    statuses: list[int] = []
    errors: list[BaseException] = []

    async def _put() -> None:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="http://test"
        ) as client:
            resp = await client.put(url, content=payload)
            statuses.append(resp.status_code)

    def _run() -> None:
        try:
            asyncio.run(_put())
        except BaseException as exc:  # noqa: BLE001 - surface any thread failure
            errors.append(exc)

    threads = [threading.Thread(target=_run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert sorted(statuses) == [204, 409]

    record = control.state.templates.get(template_id)
    assert record.is_file_uploaded(file_hash) is True
    assert file_hash not in record.upload_tokens

    archive = (
        control.state.workspace_base
        / "_builds"
        / template_id
        / "archives"
        / f"{file_hash}.tar.gz"
    )
    assert archive.is_file()
    with tarfile.open(archive, mode="r:gz") as tar:
        extracted = tar.extractfile("requirements.txt").read()
    assert extracted == b"requests==2.32.0\n"


async def test_upload_token_per_file_independent(apps):
    """Each file hash gets its own token; uploading one keeps the others."""
    control, _ = apps
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control), base_url="http://test"
    ) as client:
        info = await _create_template(client)
        template_id = info["templateID"]
        hash_a, hash_b = "hash-a", "hash-b"
        link_a = await client.get(
            f"/templates/{template_id}/files/{hash_a}",
            headers={"X-API-Key": "local-key"},
        )
        link_b = await client.get(
            f"/templates/{template_id}/files/{hash_b}",
            headers={"X-API-Key": "local-key"},
        )
        assert link_a.json()["url"] != link_b.json()["url"]
        assert (await client.put(link_a.json()["url"], content=_tar_bytes())).status_code == 204
        # hash_a is locked; hash_b's token still works.
        replay = await client.put(link_a.json()["url"], content=_tar_bytes())
        assert replay.status_code == 409
        upload_b = await client.put(link_b.json()["url"], content=_tar_bytes("pkg.txt", "x\n"))
        assert upload_b.status_code == 204


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
