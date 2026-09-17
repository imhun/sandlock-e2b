"""``E2B_IMAGE_OCI_DIR``: the tars stay shared, the extracted rootfs stays local.

Why the split exists: unpacking a rootfs onto a network filesystem is orders of
magnitude slower than onto local disk -- the same 2111-file python-slim rootfs
measured 61.4s onto the shared Aliyun NAS versus 0.26s onto the pod's local
overlay (2026-09-17) -- and that unpack runs while the sandbox's first command
waits. With the tars in one (shared) directory and the unpack in another
(node-local), every worker still sees locally built templates, but pays local
disk prices for the filesystem itself.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from envd_service.runtime import image_resolver
from envd_service.runtime.image_resolver import (
    local_oci_paths,
    peek_image_warm,
    resolve_image_rootfs,
)

IMAGE = "e2b-local/tpl_split"

def _tar_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _layer(members: dict[str, bytes]) -> bytes:
    return _tar_bytes(members)


def _oci_layout_tar(layers: list[bytes], config: bytes = b"{}") -> bytes:
    """A minimal OCI layout archive: config + layers + manifest + index."""
    blobs: dict[str, bytes] = {}

    def store(blob: bytes, media_type: str) -> tuple[str, dict]:
        digest = "sha256:" + hashlib.sha256(blob).hexdigest()
        algo, _, hexpart = digest.partition(":")
        blobs[f"blobs/{algo}/{hexpart}"] = blob
        return digest, {
            "mediaType": media_type,
            "digest": digest,
            "size": len(blob),
        }

    config_digest, _ = store(config, "application/vnd.oci.image.config.v1+json")
    descriptors = [
        store(blob, "application/vnd.oci.image.layer.v1.tar")[1] for blob in layers
    ]
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                       "digest": config_digest, "size": len(config)},
            "layers": descriptors,
        }
    ).encode()
    manifest_digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    algo, _, hexpart = manifest_digest.partition(":")
    blobs[f"blobs/{algo}/{hexpart}"] = manifest
    blobs["oci-layout"] = json.dumps({"imageLayoutVersion": "1.0.0"}).encode()
    blobs["index.json"] = json.dumps(
        {"schemaVersion": 2,
         "manifests": [{"mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": manifest_digest, "size": len(manifest)}]}
    ).encode()
    return _tar_bytes(blobs), manifest_digest


def _write_tar(dir_path: Path, image: str, payload: bytes) -> Path:
    tar_path, _ = local_oci_paths(dir_path, image)
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    tar_path.write_bytes(payload)
    return tar_path

def test_tar_from_the_producer_directory_unpacks_into_the_local_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control plane's shared tar is read, but the rootfs lands locally."""
    producer = tmp_path / "shared"
    local = tmp_path / "node-local"
    monkeypatch.setenv("E2B_IMAGE_OCI_DIR", str(producer))

    def _no_registry(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("a locally built image must not hit a registry")

    monkeypatch.setattr(image_resolver, "_client_for", _no_registry)
    payload, _digest = _oci_layout_tar([_tar_bytes({"bin/tool": b"x\n"})])
    _write_tar(producer, IMAGE, payload)

    rootfs = resolve_image_rootfs(IMAGE, local)

    assert rootfs.is_relative_to(local), rootfs
    assert (rootfs / "bin" / "tool").read_bytes() == b"x\n"
    assert (rootfs / ".complete").is_file()
    # The sidecar sits with the rootfs it points at (the local cache), so a later
    # resolve on this node finds it without touching the shared volume again.
    link = local_oci_paths(local, IMAGE)[1]
    assert link.is_file()
    assert str(rootfs) in link.read_text(encoding="utf-8")
    assert not local_oci_paths(local, IMAGE)[0].is_file()


def test_without_the_setting_the_cache_still_holds_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unset means the pre-split behavior: tar and rootfs share one directory."""
    monkeypatch.delenv("E2B_IMAGE_OCI_DIR", raising=False)
    monkeypatch.setattr(
        image_resolver,
        "_client_for",
        lambda *a, **k: pytest.fail("no registry call for a local tar"),
    )
    payload, _digest = _oci_layout_tar([_tar_bytes({"bin/tool": b"x\n"})])
    _write_tar(tmp_path, IMAGE, payload)

    rootfs = resolve_image_rootfs(IMAGE, tmp_path)

    assert rootfs.is_relative_to(tmp_path)
    assert local_oci_paths(tmp_path, IMAGE)[0].is_file()


def test_peek_reports_a_producer_tar_as_cold_but_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tar is reachable, so the node can warm it without a registry round trip."""
    producer = tmp_path / "shared"
    local = tmp_path / "node-local"
    monkeypatch.setenv("E2B_IMAGE_OCI_DIR", str(producer))
    monkeypatch.setattr(
        image_resolver,
        "_client_for",
        lambda *a, **k: pytest.fail("a local tar must not need the registry"),
    )
    payload, _digest = _oci_layout_tar([_tar_bytes({"bin/tool": b"x\n"})])
    _write_tar(producer, IMAGE, payload)

    assert peek_image_warm(IMAGE, local) == {"cached": False, "digest": None}
    rootfs = resolve_image_rootfs(IMAGE, local)
    state = peek_image_warm(IMAGE, local)
    assert state["cached"] is True
    assert (rootfs / ".complete").is_file()
