"""OCI registry client + rootfs resolver against an in-process fake registry."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import posixpath
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from envd_service.runtime.image_resolver import (
    ImageResolutionError,
    peek_image_warm,
    resolve_image_rootfs,
)
from envd_service.runtime.oci_registry import (
    parse_image_ref,
    select_platform_manifest,
)


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _tar_gz(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _tar_gz_with_symlinks(
    files: dict[str, bytes], links: dict[str, str]
) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
        for name, target in links.items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            info.size = 0
            tar.addfile(info)
    return buf.getvalue()


def _manifest_json(layers: list[dict], config_digest: str) -> dict:
    return {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "size": 2, "digest": config_digest},
        "layers": layers,
    }


class FakeRegistry:
    def __init__(self, *, require_auth: bool = False, redirect_blobs: bool = False):
        self.require_auth = require_auth
        self.redirect_blobs = redirect_blobs
        self.layers: dict[str, bytes] = {}
        self.manifests: dict[str, dict] = {}
        self.manifest_requests = 0
        self.blob_requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._make_handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server.server_port}"

    def add_layer(
        self, files: dict[str, bytes], links: dict[str, str] | None = None
    ) -> str:
        data = (
            _tar_gz(files) if not links else _tar_gz_with_symlinks(files, links)
        )
        digest = _sha256(data)
        self.layers[digest] = data
        return digest

    def add_manifest(self, tag: str, manifest: dict) -> str:
        raw = json.dumps(manifest).encode()
        self.manifests[f"{tag}"] = manifest
        self.manifests[f"{_sha256(raw)}"] = manifest
        return _sha256(raw)

    def stop(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)

    def _make_handler(self):
        # type() dict avoids the class-body scoping gotcha: a class body
        # cannot read enclosing function locals (``registry`` would resolve
        # to the module-level pytest fixture instead).
        return type("RegistryHandler", (_RegistryHandler,), {"registry": self})


class _RegistryHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence
        pass

    registry: FakeRegistry

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        if not self.registry.require_auth:
            return True
        return self.headers.get("Authorization") == "Bearer test-token"

    def do_GET(self):
        path = self.path
        if path.startswith("/token"):
            self._send(200, b'{"token": "test-token"}')
            return
        if not self._authed():
            self.send_response(401)
            self.send_header(
                "WWW-Authenticate",
                f'Bearer realm="http://{self.headers["Host"]}/token",'
                'service="fake",scope="repository:test/py:pull"',
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.startswith("/v2/test/py/manifests/"):
            self.registry.manifest_requests += 1
            tag = path.rsplit("/", 1)[1]
            manifest = self.registry.manifests.get(tag)
            if manifest is None:
                self._send(404, b"not found")
                return
            raw = json.dumps(manifest).encode()
            self._send(
                200,
                raw,
                {
                    "Content-Type": manifest.get("mediaType", "application/json"),
                    "Docker-Content-Digest": _sha256(raw),
                },
            )
            return
        if path.startswith("/v2/test/py/blobs/"):
            self.registry.blob_requests += 1
            digest = path.rsplit("/", 1)[1]
            data = self.registry.layers.get(digest)
            if data is None:
                self._send(404, b"no blob")
                return
            if self.registry.redirect_blobs:
                self.send_response(302)
                self.send_header("Location", f"/blobdata/{digest}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._send(200, data)
            return
        if path.startswith("/blobdata/"):
            digest = path.rsplit("/", 1)[1]
            self._send(200, self.registry.layers.get(digest, b""))
            return
        self._send(404, b"not found")


@pytest.fixture()
def registry():
    reg = FakeRegistry()
    yield reg
    reg.stop()


def _base_registry(registry: FakeRegistry, tmp_path: Path):
    config_digest = _sha256(b"{}")
    l1 = registry.add_layer({"bin/sh": b"#!/bin/sh\n", "etc/os-release": b"ID=debian\n"})
    manifest = _manifest_json(
        [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "size": 1, "digest": l1}],
        config_digest,
    )
    registry.add_manifest("latest", manifest)
    return f"{registry.host}/test/py:latest"


def test_parse_image_ref_variants():
    assert parse_image_ref("python:3.14-slim").host == "registry-1.docker.io"
    assert parse_image_ref("python:3.14-slim").repository == "library/python"
    assert parse_image_ref("imhun/sandlock:tag").repository == "imhun/sandlock"
    ref = parse_image_ref("registry.cn-shanghai.aliyuncs.com/byteplan/x:1")
    assert ref.host == "registry.cn-shanghai.aliyuncs.com"
    assert ref.repository == "byteplan/x"
    assert parse_image_ref("x@sha256:abc").reference == "sha256:abc"


def test_parse_image_ref_tag_plus_digest_keeps_the_repository_clean():
    """``repo:tag@sha256:...`` is the E6.2 production form (upgrade.sh refuses a
    tag-only base image). The tag is a human hint: the registry is asked for the
    digest, so it must not survive into the repository path -- that produced
    ``/v2/<repo>:<tag>/manifests/sha256:...`` and a 404 (measured against ACR on
    2026-09-15, which made every sandbox create fail on the deployed worker)."""
    digest = "sha256:" + "a" * 64
    ref = parse_image_ref(
        f"registry.cn-shanghai.aliyuncs.com/byteplan/python-mcp:3.14@{digest}"
    )
    assert ref.host == "registry.cn-shanghai.aliyuncs.com"
    assert ref.repository == "byteplan/python-mcp"
    assert ref.reference == digest
    assert ref.is_digest
    # A registry host that carries a port must keep it: the tag stripper works
    # on the last path segment only.
    ref = parse_image_ref(f"localhost:5000/ns/img:1@{digest}")
    assert ref.host == "localhost:5000"
    assert ref.repository == "ns/img"
    # Docker Hub short form, and a digest without any tag.
    assert parse_image_ref(f"python:3.14-slim@{digest}").repository == "library/python"
    assert parse_image_ref(f"python@{digest}").repository == "library/python"


def test_resolve_rootfs_from_a_digest_pinned_ref(registry, tmp_path):
    """End to end: a digest-pinned reference resolves through the same code path
    the worker uses (this is the shape E6.2 requires in production)."""
    from envd_service.runtime.oci_registry import RegistryClient

    image = _base_registry(registry, tmp_path)
    repo, tag = image.rsplit(":", 1)
    _, digest = RegistryClient(parse_image_ref(image), scheme="http").manifest()
    assert digest.startswith("sha256:")
    rootfs = resolve_image_rootfs(f"{repo}:{tag}@{digest}", tmp_path)
    assert (rootfs / "bin" / "sh").is_file()


def test_resolve_extracts_rootfs_and_caches(registry, tmp_path):
    image = _base_registry(registry, tmp_path)
    rootfs = resolve_image_rootfs(image, tmp_path)
    assert (rootfs / "bin" / "sh").is_file()
    assert (rootfs / "etc" / "os-release").read_text() == "ID=debian\n"
    assert (rootfs / ".complete").is_file()

    blob_requests_after_first = registry.blob_requests
    again = resolve_image_rootfs(image, tmp_path)
    assert again == rootfs
    # Manifest is re-fetched (to compute the current digest), but cached
    # rootfs skips the blob downloads and extraction.
    assert registry.blob_requests == blob_requests_after_first


def test_resolve_applies_whiteouts(registry, tmp_path):
    config_digest = _sha256(b"{}")
    l1 = registry.add_layer(
        {"bin/sh": b"#!/bin/sh\n", "a.txt": b"old", "sub/x.txt": b"x"}
    )
    l2 = registry.add_layer(
        {".wh.a.txt": b"", "sub/.wh..wh..opq": b"", "sub/y.txt": b"y"}
    )
    manifest = _manifest_json(
        [
            {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "size": 1, "digest": l1},
            {"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "size": 1, "digest": l2},
        ],
        config_digest,
    )
    registry.add_manifest("latest", manifest)
    rootfs = resolve_image_rootfs(f"{registry.host}/test/py:latest", tmp_path)
    assert not (rootfs / "a.txt").exists()
    assert not (rootfs / "sub" / "x.txt").exists()
    assert (rootfs / "sub" / "y.txt").read_text() == "y"


def _inode(path: Path) -> tuple[int, int]:
    stat = os.stat(path)
    return (stat.st_dev, stat.st_ino)


def _resolved_in_chroot(rootfs: Path, link: Path) -> Path:
    """The path the sandbox's ``RESOLVE_IN_ROOT`` lookup lands on.

    An absolute target anchors at the chroot root and a relative one at the
    link's own directory; ``..`` is clamped at the root, which is exactly what
    ``os.path.normpath`` does to the absolute spelling (it cannot climb above
    ``/``).
    """
    target = os.readlink(link)
    if os.path.isabs(target):
        spelling = target
    else:
        spelling = posixpath.join("/", str(link.relative_to(rootfs).parent), target)
    return rootfs / os.path.normpath(spelling).lstrip("/")


def test_resolve_keeps_symlink_targets_resolving_inside_the_chroot(
    registry, tmp_path
):
    """Image-rootfs fidelity: absolute symlink targets (e.g. ``/usr/lib/
    ssl/cert.pem -> /etc/ssl/certs/ca-certificates.crt``) must survive
    extraction and keep resolving to the *rootfs* copy in the sandbox's view.
    tarfile's ``data`` filter drops raw absolute links, which would break
    Python's default CA path resolution inside the chroot
    (``ssl.get_default_verify_paths()``).

    The extracted tree is handed to the sandbox as a chroot root, and every
    path lookup inside it is ``openat2(RESOLVE_IN_ROOT)``: an absolute target
    resolves at the sandbox root, exactly like the relative link the extractor
    writes to get past the ``data`` filter. Those targets keep their ``..``
    components -- the resolver used to rewrite them into the rooted absolute
    equivalent (FUP-28) and no longer does, so the ``..`` walk is the engine's
    to retry (fork FUP-26). What has to hold either way is the inode: the link
    lands on the rootfs copy, never on the host's ``/etc``.
    """
    config_digest = _sha256(b"{}")
    l1 = registry.add_layer(
        {
            "etc/ssl/certs/ca-certificates.crt": b"CA\n",
            "usr/bin/sh": b"#!/bin/sh\n",
        },
        links={
            "usr/lib/ssl/cert.pem": "/etc/ssl/certs/ca-certificates.crt",
            "usr/lib/ssl/certs": "/etc/ssl/certs",
        },
    )
    manifest = _manifest_json(
        [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "size": 1,
                "digest": l1,
            }
        ],
        config_digest,
    )
    registry.add_manifest("latest", manifest)
    rootfs = resolve_image_rootfs(f"{registry.host}/test/py:latest", tmp_path)

    cert_link = rootfs / "usr/lib/ssl/cert.pem"
    assert cert_link.is_symlink()
    target = cert_link.readlink()
    assert not target.is_absolute()
    assert ".." in target.parts
    assert _inode(_resolved_in_chroot(rootfs, cert_link)) == _inode(
        rootfs / "etc/ssl/certs/ca-certificates.crt"
    )
    assert _resolved_in_chroot(rootfs, cert_link).read_text() == "CA\n"

    certs_link = rootfs / "usr/lib/ssl/certs"
    assert certs_link.is_symlink()
    certs_target = certs_link.readlink()
    assert not certs_target.is_absolute()
    assert ".." in certs_target.parts
    assert _inode(_resolved_in_chroot(rootfs, certs_link)) == _inode(
        rootfs / "etc/ssl/certs"
    )

    # The whole extracted tree, link by link: the extractor's relative targets
    # survive (the retired rewrite is what turned them rooted-absolute, and
    # this is the guard that it does not come back), no link is absolute (the
    # host-side spelling of the tree stays inside it), and every link resolves
    # to the inode the image meant.
    expected = {
        "usr/lib/ssl/cert.pem": rootfs / "etc/ssl/certs/ca-certificates.crt",
        "usr/lib/ssl/certs": rootfs / "etc/ssl/certs",
    }
    links = sorted(path for path in rootfs.rglob("*") if path.is_symlink())
    assert [str(path.relative_to(rootfs)) for path in links] == sorted(expected)
    for link in links:
        assert not Path(os.readlink(link)).is_absolute()
        assert _inode(_resolved_in_chroot(rootfs, link)) == _inode(
            expected[str(link.relative_to(rootfs))]
        )


def test_resolve_with_bearer_auth_and_redirect(registry, tmp_path):
    registry.require_auth = True
    registry.redirect_blobs = True
    image = _base_registry(registry, tmp_path)
    rootfs = resolve_image_rootfs(image, tmp_path)
    assert (rootfs / "bin" / "sh").is_file()
    assert registry.blob_requests >= 1


def test_select_platform_manifest_prefers_current_arch():
    index = {
        "manifests": [
            {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
        ]
    }
    assert select_platform_manifest(index, arch="amd64")["digest"] == "sha256:amd"
    assert select_platform_manifest(index, arch="arm64")["digest"] == "sha256:arm"


def test_peek_warm_reflects_cache(registry, tmp_path):
    image = _base_registry(registry, tmp_path)
    assert peek_image_warm(image, tmp_path)["cached"] is False
    resolve_image_rootfs(image, tmp_path)
    assert peek_image_warm(image, tmp_path)["cached"] is True
    assert peek_image_warm("", tmp_path)["cached"] is False


def test_unknown_image_fails(registry, tmp_path):
    with pytest.raises(ImageResolutionError):
        resolve_image_rootfs(f"{registry.host}/test/py:missing", tmp_path)


def test_blob_digest_mismatch_fails_closed_as_retryable(registry):
    """A corrupt/truncated layer must never unpack into a rootfs: the blob()
    call verifies sha256 against the requested digest and raises a retryable
    RegistryError so the mirror chain falls through to the next endpoint."""
    from envd_service.runtime.oci_registry import RegistryClient, RegistryError

    l1 = registry.add_layer({"bin/true": b"\x7fELF-corruptible"})
    ref = parse_image_ref(f"{registry.host}/test/py:latest")
    client = RegistryClient(ref, scheme="http")
    # Serve different bytes than the digest advertises (truncated-layer shape).
    registry.layers[l1] = b"corrupted-bytes"
    with pytest.raises(RegistryError) as ei:
        client.blob(l1)
    assert ei.value.retryable is True
    assert "digest mismatch" in str(ei.value)
    assert f"sha256:{hashlib.sha256(b'corrupted-bytes').hexdigest()}" in str(ei.value)


def test_anonymous_basic_challenge_does_not_write_none_authorization(monkeypatch):
    """An anonymous pull facing a Basic-only mirror must not write a None
    Authorization header after the challenge (httpx TypeError used to abort
    the whole fetch instead of falling through to the next endpoint)."""
    import httpx

    from envd_service.runtime.oci_registry import RegistryClient

    calls: list[object] = []

    def fake_request(method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        calls.append(headers.get("Authorization"))
        req = httpx.Request("GET", url)
        if len(calls) == 1:
            return httpx.Response(
                401,
                headers={"WWW-Authenticate": 'Basic realm="fake"'},
                request=req,
            )
        return httpx.Response(200, content=b"{}", request=req)

    monkeypatch.setattr(httpx, "request", fake_request)
    client = RegistryClient(parse_image_ref("example.com/test/py:latest"))
    manifest, digest = client.manifest()
    assert manifest == {}
    assert digest == "sha256:" + hashlib.sha256(b"{}").hexdigest()
    assert len(calls) == 2
    assert calls[0] is None, "no credentials before the challenge"
    assert calls[1] is None, "anonymous Basic challenge must not attach a None header"


def test_platform_digest_is_cached_within_the_ttl(monkeypatch, tmp_path):
    """One tag is looked up once per TTL window, not once per create."""
    from envd_service.runtime import image_resolver

    calls: list[str] = []

    def fake_fetch(client):
        calls.append(client._ref.reference)
        return {"layers": []}, "sha256:aaaa"

    monkeypatch.setattr(image_resolver, "fetch_platform_manifest", fake_fetch)
    monkeypatch.setenv("E2B_IMAGE_MANIFEST_TTL_S", "60")
    image_resolver._DIGEST_CACHE.clear()

    for _ in range(5):
        assert image_resolver._platform_digest(
            "python:3.11-slim", registry_username=None, registry_password=None
        ) == "sha256:aaaa"
    assert len(calls) == 1

    # Credentials and scheme take part in the key: a different lookup path is
    # not answered from the previous one.
    image_resolver._platform_digest(
        "python:3.11-slim", registry_username="u", registry_password="p"
    )
    assert len(calls) == 2
    image_resolver._DIGEST_CACHE.clear()


def test_platform_digest_ttl_zero_always_refetches(monkeypatch):
    from envd_service.runtime import image_resolver

    calls: list[str] = []

    def fake_fetch(client):
        calls.append(client._ref.reference)
        return {"layers": []}, "sha256:bbbb"

    monkeypatch.setattr(image_resolver, "fetch_platform_manifest", fake_fetch)
    monkeypatch.setenv("E2B_IMAGE_MANIFEST_TTL_S", "0")
    image_resolver._DIGEST_CACHE.clear()

    for _ in range(3):
        image_resolver._platform_digest(
            "python:3.11-slim", registry_username=None, registry_password=None
        )
    assert len(calls) == 3
    image_resolver._DIGEST_CACHE.clear()


# --- mirror / endpoint fallback / credential scoping ------------------------


class _RecordingResponse:
    def __init__(self, status: int = 200, body: bytes = b"{}", headers=None) -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode()
        self.headers = headers or {}

    def json(self) -> dict:
        return json.loads(self.content)


def _recorder(responses):
    """Fake ``httpx.request`` returning ``responses`` per URL, in call order."""
    import httpx

    calls: list[tuple[str, dict[str, str]]] = []
    queue = list(responses)

    def fake_request(method, url, headers=None, **kwargs):
        calls.append((url, dict(headers or {})))
        assert queue, f"unexpected extra request to {url}"
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    return fake_request, calls


def test_registry_mirrors_parsing(monkeypatch):
    from envd_service.runtime import oci_registry

    monkeypatch.setenv(
        "E2B_REGISTRY_MIRRORS",
        "https://registry-1.docker.io/=docker.m.daocloud.io|docker.1ms.run,"
        "gcr.io=mirror.gcr.io",
    )
    assert oci_registry.registry_mirrors() == {
        "registry-1.docker.io": ["docker.m.daocloud.io", "docker.1ms.run"],
        "gcr.io": ["mirror.gcr.io"],
    }
    assert oci_registry.registry_mirrors_for("registry-1.docker.io") == [
        "docker.m.daocloud.io",
        "docker.1ms.run",
    ]
    assert oci_registry.registry_mirrors_for("quay.io") == []


def test_mirrors_default_to_the_multi_source_chain_when_unset(monkeypatch):
    """Unset = the built-in chain; explicitly empty still means "pull direct"."""
    from envd_service.runtime import oci_registry

    monkeypatch.delenv("E2B_REGISTRY_MIRRORS", raising=False)
    assert oci_registry.registry_mirrors() == {
        "registry-1.docker.io": ["docker.m.daocloud.io", "docker.1ms.run"],
    }
    # The lookup normalizes the source the same way parse_image_ref does, so
    # the default bucket is hit by a plain host name too.
    assert oci_registry.registry_mirrors_for("https://registry-1.docker.io/") == [
        "docker.m.daocloud.io",
        "docker.1ms.run",
    ]

    monkeypatch.setenv("E2B_REGISTRY_MIRRORS", "")
    assert oci_registry.registry_mirrors() == {}
    assert oci_registry.registry_mirrors_for("registry-1.docker.io") == []


def test_unset_env_still_prefers_the_default_mirror_chain(monkeypatch):
    """The default path is multi-source: two mirrors, origin last."""
    import httpx

    from envd_service.runtime import oci_registry

    monkeypatch.delenv("E2B_REGISTRY_MIRRORS", raising=False)
    fake, calls = _recorder(
        [
            _RecordingResponse(429, b'{"errors":[{"code":"TOOMANYREQUESTS"}]}'),
            _RecordingResponse(429, b'{"errors":[{"code":"TOOMANYREQUESTS"}]}'),
            _RecordingResponse(body=json.dumps({"layers": []}).encode()),
        ]
    )
    monkeypatch.setattr(httpx, "request", fake)

    client = oci_registry.RegistryClient(
        parse_image_ref("python:3.11-slim"), timeout=5
    )
    assert client.manifest()[0] == {"layers": []}
    assert [url.split("//", 1)[1].split("/")[0] for url, _ in calls] == [
        "docker.m.daocloud.io",
        "docker.1ms.run",
        "registry-1.docker.io",
    ]


def test_pull_prefers_the_mirror(monkeypatch):
    import httpx

    from envd_service.runtime import oci_registry

    monkeypatch.setenv("E2B_REGISTRY_MIRRORS", "registry-1.docker.io=mirror.example")
    fake, calls = _recorder([_RecordingResponse(body=json.dumps({"layers": []}).encode())])
    monkeypatch.setattr(httpx, "request", fake)

    client = oci_registry.RegistryClient(
        parse_image_ref("python:3.11-slim"), timeout=5
    )
    client.manifest()
    assert calls[0][0] == (
        "https://mirror.example/v2/library/python/manifests/3.11-slim"
    )


def test_dead_mirror_falls_back_to_the_origin(monkeypatch):
    import httpx

    from envd_service.runtime import oci_registry

    monkeypatch.setenv(
        "E2B_REGISTRY_MIRRORS", "registry-1.docker.io=dead.example|also-dead.example"
    )
    fake, calls = _recorder(
        [
            httpx.ConnectError("no route"),
            _RecordingResponse(429, b'{"errors":[{"code":"TOOMANYREQUESTS"}]}'),
            _RecordingResponse(body=json.dumps({"layers": []}).encode()),
        ]
    )
    monkeypatch.setattr(httpx, "request", fake)

    client = oci_registry.RegistryClient(
        parse_image_ref("python:3.11-slim"), timeout=5
    )
    assert client.manifest()[0] == {"layers": []}
    assert [url.split("//", 1)[1].split("/")[0] for url, _ in calls] == [
        "dead.example",
        "also-dead.example",
        "registry-1.docker.io",
    ]


def test_missing_tag_is_not_retried_against_the_origin(monkeypatch):
    """A 404 is an answer, not an endpoint problem: no extra mirror round-trip."""
    import httpx

    from envd_service.runtime import oci_registry

    monkeypatch.setenv("E2B_REGISTRY_MIRRORS", "registry-1.docker.io=mirror.example")
    fake, calls = _recorder([_RecordingResponse(404, b"{}")])
    monkeypatch.setattr(httpx, "request", fake)

    client = oci_registry.RegistryClient(parse_image_ref("python:9.99"), timeout=5)
    with pytest.raises(oci_registry.RegistryError, match="404"):
        client.manifest()
    assert len(calls) == 1


def test_registry_credentials_stay_on_their_own_host(monkeypatch):
    import httpx

    from envd_service.runtime import oci_registry

    monkeypatch.delenv("E2B_REGISTRY_MIRRORS", raising=False)
    monkeypatch.setenv("E2B_IMAGE_REGISTRY", "acr.example.com/e2b")
    fake, calls = _recorder(
        [
            # Docker Hub challenges; the ACR password must not be sent there.
            _RecordingResponse(
                401,
                b"{}",
                {"WWW-Authenticate": 'Bearer realm="https://token.example/token"'},
            ),
            _RecordingResponse(body=json.dumps({"layers": []}).encode()),
        ]
    )
    monkeypatch.setattr(httpx, "request", fake)
    token_calls: list[str] = []

    def fake_get(url, **kwargs):
        token_calls.append(url)
        return _RecordingResponse(body=json.dumps({"token": "tok-1"}).encode())

    monkeypatch.setattr(httpx, "get", fake_get)

    client = oci_registry.RegistryClient(
        parse_image_ref("python:3.11-slim"),
        username="acr-user",
        password="acr-secret",
        timeout=5,
    )
    client.manifest()
    assert client._username is None
    assert len(token_calls) == 1
    assert token_calls[0].startswith("https://token.example/token?")
    assert "scope=repository%3Alibrary/python%3Apull" in token_calls[0]
    assert "Authorization" not in calls[0][1]
    assert calls[1][1]["Authorization"] == "Bearer tok-1"
    assert all("acr-secret" not in json.dumps(headers) for _, headers in calls)

    same_host = oci_registry.RegistryClient(
        parse_image_ref("acr.example.com/e2b/tpl_1:latest"),
        username="acr-user",
        password="acr-secret",
        timeout=5,
    )
    assert same_host._username == "acr-user"


def test_explicit_credential_host_overrides_the_environment(monkeypatch):
    """Callers pass the host their own Settings name (fixtures, not env)."""
    from envd_service.runtime import oci_registry

    monkeypatch.delenv("E2B_IMAGE_REGISTRY", raising=False)
    ref = parse_image_ref("127.0.0.1:5000/tpl_1:latest")
    matched = oci_registry.RegistryClient(
        ref,
        username="u",
        password="p",
        credential_host="127.0.0.1:5000",
        timeout=5,
    )
    assert matched._username == "u"

    other = oci_registry.RegistryClient(
        ref,
        username="u",
        password="p",
        credential_host="registry.example.com",
        timeout=5,
    )
    assert other._username is None
    assert other._password is None


def test_the_resolved_digest_survives_the_process(monkeypatch, tmp_path):
    """One resolution has to be enough for the *next* process too (N54).

    The in-process cache below dies with the worker process -- and it is
    per-process by construction, so two workers on one node each pay their own
    lookup. Measured on the fleet 2026-10-01: every create still spent ~6 HTTPS
    round trips (auth + manifest, ~0.5 s) resolving the *same* pinned base
    image, because the answer only ever lived in memory. Warm the node once (or
    let one create resolve it) and the result has to be on disk.
    """
    from envd_service.runtime import image_resolver

    calls: list[str] = []

    def fake_fetch(client):
        calls.append(client._ref.reference)
        return {"layers": []}, "sha256:aaaa"

    monkeypatch.setattr(image_resolver, "fetch_platform_manifest", fake_fetch)
    monkeypatch.setenv("E2B_IMAGE_MANIFEST_TTL_S", "60")
    image_resolver._DIGEST_CACHE.clear()

    assert (
        image_resolver._platform_digest(
            "python:3.11-slim",
            registry_username=None,
            registry_password=None,
            cache_dir=tmp_path,
        )
        == "sha256:aaaa"
    )
    assert len(calls) == 1

    # A fresh process (empty in-process cache) reads it from the cache dir.
    image_resolver._DIGEST_CACHE.clear()
    assert (
        image_resolver._platform_digest(
            "python:3.11-slim",
            registry_username=None,
            registry_password=None,
            cache_dir=tmp_path,
        )
        == "sha256:aaaa"
    )
    assert len(calls) == 1, "the second lookup went to the registry again"

    # Credentials still take part in the key -- a different lookup path must not
    # be answered from the persisted one.
    assert (
        image_resolver._platform_digest(
            "python:3.11-slim",
            registry_username="u",
            registry_password="p",
            cache_dir=tmp_path,
        )
        == "sha256:aaaa"
    )
    assert len(calls) == 2
    image_resolver._DIGEST_CACHE.clear()


def test_an_extracted_rootfs_needs_no_manifest_lookup(monkeypatch, tmp_path):
    """The digest on disk is enough for a resolve whose rootfs is already there.

    ``resolve_image_rootfs`` used to fetch the platform manifest unconditionally
    once the local/shared/tar paths missed -- which is the second half of the
    per-create cost (the first half is the peek). With the digest persisted, an
    extracted rootfs is found without asking the registry anything.
    """
    from envd_service.runtime import image_resolver

    image = "python:3.11-slim"

    def explode(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("resolve must not look up the manifest")

    monkeypatch.setattr(image_resolver, "fetch_platform_manifest", explode)
    monkeypatch.setenv("E2B_IMAGE_MANIFEST_TTL_S", "60")
    image_resolver._DIGEST_CACHE.clear()

    # Seed what a warm would have left behind: the digest, and an extracted
    # rootfs for it.
    digest = "sha256:cccc"
    image_resolver._write_digest_cache(tmp_path, image, None, None, digest)
    rootfs = image_resolver._cache_rootfs(tmp_path, image, digest)
    rootfs.mkdir(parents=True)
    (rootfs / ".complete").write_text("", encoding="utf-8")

    assert image_resolver.resolve_image_rootfs(image, tmp_path) == rootfs
    image_resolver._DIGEST_CACHE.clear()


def test_the_persisted_digest_cache_survives_a_prune(monkeypatch, tmp_path):
    """The prune may not reclaim the digest cache it exists to keep (N54).

    ``_cache_usage`` classifies *every* dot-prefixed child as a staging tree and
    the prune reclaims those once they are older than the staleness window -- so
    a persisted digest written by a warm would be deleted an hour later and
    every create would go back to paying the registry round trips.
    """
    from envd_service.runtime import image_resolver

    image = "python:3.11-slim"
    image_resolver._write_digest_cache(tmp_path, image, None, None, "sha256:dddd")
    key = image_resolver._digest_cache_key(image, None, None, None)
    path = image_resolver._digest_cache_file(tmp_path, key)
    assert path.is_file()

    # Age it well past every staleness window the prune knows.
    old = time.time() - 10_000
    os.utime(path, (old, old))
    os.utime(path.parent, (old, old))

    image_resolver.prune_image_cache(tmp_path)

    assert path.is_file(), "the prune reclaimed the persisted digest"
    assert (
        image_resolver._read_digest_cache(tmp_path, image, None, None)
        == "sha256:dddd"
    )
