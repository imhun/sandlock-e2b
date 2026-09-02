"""Locally built template images (no ``E2B_IMAGE_REGISTRY``) resolve from the
OCI layout tar that ``Template.build`` exports, without a registry call."""

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

IMAGE = "e2b-local/tpl_local"


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


def _write_local_oci(cache: Path, image: str, payload: bytes) -> Path:
    tar_path, _link = local_oci_paths(cache, image)
    tar_path.parent.mkdir(parents=True, exist_ok=True)
    tar_path.write_bytes(payload)
    return tar_path


def test_local_oci_paths_are_image_scoped(tmp_path: Path) -> None:
    tar_path, link = local_oci_paths(tmp_path, IMAGE)
    assert tar_path == tmp_path / "_oci" / "e2b-local_tpl_local.oci.tar"
    assert link == tmp_path / "_oci" / "e2b-local_tpl_local.link"


def test_resolve_extracts_layers_with_whiteouts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Layers come out of the tar (whiteouts honored) and no registry is asked."""
    def _no_registry(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("local OCI image must not hit a registry")

    monkeypatch.setattr(image_resolver, "_client_for", _no_registry)
    lower = _layer({"etc/hosts": b"1.2.3.4 host\n", "etc/old": b"gone\n"})
    upper = _layer(
        {
            "etc/hosts": b"5.6.7.8 host\n",
            "bin/sh": b"#!/bin/sh\n",
            "app/run.sh": b"#!/bin/sh\n",
            "app/stale": b"dropped by the next layer\n",
        }
    )
    # OCI whiteouts: .wh.<name> removes an entry a lower layer added.
    whiteout = _layer({"etc/.wh.old": b"", "app/.wh.stale": b""})
    payload, digest = _oci_layout_tar([lower, upper, whiteout])
    tar_path = _write_local_oci(tmp_path, IMAGE, payload)

    rootfs = resolve_image_rootfs(IMAGE, tmp_path)

    assert (rootfs / "etc" / "hosts").read_bytes() == b"5.6.7.8 host\n"
    assert (rootfs / "app" / "run.sh").is_file()
    assert not (rootfs / "etc" / "old").exists()
    assert not (rootfs / "app" / "stale").exists()
    assert (rootfs / ".complete").is_file()
    assert rootfs.parent.name == f"e2b-local_tpl_local-{digest[7:47]}"
    # The sidecar records the digest + path so later resolves skip the tar.
    assert tmp_path.joinpath("_oci", "e2b-local_tpl_local.link").read_text(
        encoding="utf-8"
    ).splitlines() == [digest, str(rootfs)]

    tar_path.unlink()
    assert resolve_image_rootfs(IMAGE, tmp_path) == rootfs


def test_peek_warm_tracks_the_local_tar_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _no_registry(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("local OCI image must not hit a registry")

    monkeypatch.setattr(image_resolver, "_client_for", _no_registry)
    payload, digest = _oci_layout_tar([_layer({"bin/tool": b"x\n"})])
    _write_local_oci(tmp_path, IMAGE, payload)

    assert peek_image_warm(IMAGE, tmp_path) == {"cached": False, "digest": None}
    resolve_image_rootfs(IMAGE, tmp_path)
    assert peek_image_warm(IMAGE, tmp_path) == {"cached": True, "digest": digest}


def test_unreadable_layout_reports_which_file_is_broken(tmp_path: Path) -> None:
    """A truncated/foreign tar fails as an image error, not a registry call."""
    tar_path = _write_local_oci(tmp_path, IMAGE, _tar_bytes({"README": b"nope"}))
    with pytest.raises(Exception) as excinfo:
        resolve_image_rootfs(IMAGE, tmp_path)
    assert not isinstance(excinfo.value, image_resolver.RegistryError)
    assert tar_path.is_file()


def test_empty_rootfs_is_not_marked_complete(tmp_path: Path) -> None:
    """A layout whose layers add no filesystem fails instead of being cached."""
    from envd_service.runtime.image_resolver import ImageResolutionError

    payload, _digest = _oci_layout_tar([_layer({"README": b"no filesystem here\n"})])
    _write_local_oci(tmp_path, "e2b-local/tpl_empty", payload)
    with pytest.raises(ImageResolutionError, match="empty rootfs"):
        resolve_image_rootfs("e2b-local/tpl_empty", tmp_path)
    assert peek_image_warm("e2b-local/tpl_empty", tmp_path)["cached"] is False
