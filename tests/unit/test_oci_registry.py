"""OCI registry client + rootfs resolver against an in-process fake registry."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import posixpath
import tarfile
import threading
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


def test_resolve_preserves_absolute_symlinks_as_relative(registry, tmp_path):
    """Image-rootfs fidelity: absolute symlink targets (e.g. ``/usr/lib/
    ssl/cert.pem -> /etc/ssl/certs/ca-certificates.crt``) must survive
    extraction as chroot-safe relative links. tarfile's ``data`` filter
    drops raw absolute links, which would break Python's default CA path
    resolution inside the chroot (``ssl.get_default_verify_paths()``)."""
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
    assert not posixpath.isabs(target)
    assert (cert_link.parent / target).resolve() == (
        rootfs / "etc/ssl/certs/ca-certificates.crt"
    ).resolve()
    assert (cert_link.parent / target).read_text() == "CA\n"

    certs_link = rootfs / "usr/lib/ssl/certs"
    assert certs_link.is_symlink()
    assert (certs_link.parent / certs_link.readlink()).resolve() == (
        rootfs / "etc/ssl/certs"
    ).resolve()


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
