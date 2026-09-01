"""Minimal OCI Distribution (registry v2) client without a container daemon.

Used by :mod:`envd_service.runtime.image_resolver` to resolve a base image
into an extracted rootfs: fetch the (platform-resolved) manifest, download
the layer blobs, and assemble the filesystem with OCI whiteout semantics.
Supports anonymous pulls, Basic auth and Bearer-token challenge (Docker Hub /
Aliyun ACR), and follows blob redirects (Aliyun hands blobs off to OSS/CDN).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import platform
import posixpath
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

DOCKER_HUB_HOST = "registry-1.docker.io"
DOCKER_HUB_LIBRARY = "library"

_MANIFEST_ACCEPT = ", ".join(
    [
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
    ]
)

_INDEX_MEDIA_TYPES = {
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.index.v1+json",
}


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageRef:
    host: str
    repository: str
    reference: str  # tag or digest (without @)

    @property
    def is_digest(self) -> bool:
        return self.reference.startswith("sha256:")


def _default_scheme(host: str) -> str:
    if host.startswith("localhost") or host.startswith("127.0.0.1"):
        return "http"
    return "https"


def parse_image_ref(image: str) -> ImageRef:
    """Parse an image reference, expanding Docker Hub short names.

    ``python:3.14-slim`` -> registry-1.docker.io/library/python:3.14-slim
    ``imhun/sandlock:tag`` -> registry-1.docker.io/imhun/sandlock:tag
    ``host:5000/ns/img@sha256:...`` -> explicit host + digest.
    """
    if "@" in image:
        repo, reference = image.rsplit("@", 1)
    else:
        repo, _, tag = image.rpartition(":")
        reference = tag if _looks_like_tag(tag) else "latest"
        if not _looks_like_tag(tag):
            repo = image
    parts = repo.split("/")
    first = parts[0]
    if len(parts) == 1 or (
        len(parts) >= 2
        and "." not in first
        and ":" not in first
        and first != "localhost"
    ):
        # Docker Hub: single name gets the library/ prefix, namespaces do not.
        host = DOCKER_HUB_HOST
        repository = (
            f"{DOCKER_HUB_LIBRARY}/{repo}" if len(parts) == 1 else repo
        )
    else:
        host = first
        repository = "/".join(parts[1:])
    return ImageRef(host=host, repository=repository, reference=reference)


def _looks_like_tag(tag: str) -> bool:
    return bool(tag) and ":" not in tag and "/" not in tag


class RegistryClient:
    """Fetch manifests and blobs from one registry with auth handling."""

    def __init__(
        self,
        ref: ImageRef,
        *,
        username: str | None = None,
        password: str | None = None,
        scheme: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._ref = ref
        self._username = username
        self._password = password
        self._scheme = scheme or _default_scheme(ref.host)
        self._timeout = timeout
        self._token: str | None = None
        self._basic_auth: tuple[str, str] | None = None

    @property
    def _base(self) -> str:
        return f"{self._scheme}://{self._ref.host}/v2"

    def _authorization(self) -> str | None:
        if self._token:
            return f"Bearer {self._token}"
        if self._basic_auth and self._username:
            import base64

            raw = f"{self._username}:{self._password}".encode()
            return f"Basic {base64.b64encode(raw).decode()}"
        return None

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault("Accept", _MANIFEST_ACCEPT)
        auth_header = self._authorization()
        if auth_header:
            headers["Authorization"] = auth_header
        resp = httpx.request(
            method,
            url,
            headers=headers,
            timeout=self._timeout,
            follow_redirects=True,
            **kwargs,
        )
        if resp.status_code in (401, 403) and not self._token:
            self._challenge(resp)
            headers["Authorization"] = self._authorization()
            resp = httpx.request(
                method,
                url,
                headers=headers,
                timeout=self._timeout,
                follow_redirects=True,
                **kwargs,
            )
        if resp.status_code >= 400:
            raise RegistryError(
                f"registry {self._ref.host} {method} {url} -> {resp.status_code}: "
                f"{resp.text[:300]}"
            )
        return resp

    def _challenge(self, resp: httpx.Response) -> None:
        header = resp.headers.get("WWW-Authenticate", "")
        if header.lower().startswith("basic"):
            if self._username:
                self._basic_auth = (self._username, self._password or "")
            return
        if header.lower().startswith("bearer"):
            params: dict[str, str] = {}
            for part in header[len("Bearer "):].split(","):
                if "=" in part:
                    key, _, value = part.partition("=")
                    params[key.strip().lower()] = value.strip().strip('"')
            realm = params.get("realm")
            if not realm:
                raise RegistryError("registry Bearer challenge without realm")
            scope = params.get("scope", f"repository:{self._ref.repository}:pull")
            token_url = f"{realm}?service={quote(params.get('service', ''))}&scope={quote(scope)}"
            auth: httpx.Auth | None = None
            if self._username:
                auth = (self._username, self._password or "")
            token_resp = httpx.get(
                token_url,
                auth=auth,
                timeout=self._timeout,
                follow_redirects=True,
            )
            if token_resp.status_code >= 400:
                raise RegistryError(
                    f"registry token exchange failed: {token_resp.status_code}: "
                    f"{token_resp.text[:300]}"
                )
            self._token = token_resp.json().get("token") or token_resp.json().get(
                "access_token"
            )
            if not self._token:
                raise RegistryError("registry token response missing token")
            return
        if self._username:
            self._basic_auth = (self._username, self._password or "")

    def manifest(self, reference: str | None = None) -> tuple[dict[str, Any], str]:
        """Return (manifest dict, content digest)."""
        ref = reference or self._ref.reference
        url = (
            f"{self._base}/{self._ref.repository}"
            f"/manifests/{quote(ref, safe=':')}"
        )
        resp = self._request("GET", url)
        try:
            manifest = resp.json()
        except json.JSONDecodeError as e:
            raise RegistryError(f"invalid manifest JSON from {url}: {e}") from e
        digest = resp.headers.get("Docker-Content-Digest")
        if not digest:
            digest = "sha256:" + hashlib.sha256(resp.content).hexdigest()
        return manifest, digest

    def blob(self, digest: str) -> bytes:
        url = f"{self._base}/{self._ref.repository}/blobs/{digest}"
        return self._request("GET", url).content


def _machine_platform() -> tuple[str, str]:
    arch = platform.machine().lower()
    if arch in ("x86_64", "amd64"):
        return "linux", "amd64"
    if arch in ("aarch64", "arm64"):
        return "linux", "arm64"
    return "linux", arch


def select_platform_manifest(
    index: dict[str, Any],
    *,
    os_name: str | None = None,
    arch: str | None = None,
) -> dict[str, Any]:
    """Pick the platform entry from a manifest list / OCI index."""
    os_name = os_name or _machine_platform()[0]
    arch = arch or _machine_platform()[1]
    for entry in index.get("manifests", []):
        plat = entry.get("platform") or {}
        if plat.get("os") == os_name and plat.get("architecture") == arch:
            return entry
    for entry in index.get("manifests", []):
        plat = entry.get("platform") or {}
        if plat.get("os") == os_name:
            return entry
    raise RegistryError(
        f"no manifest for {os_name}/{arch} in index with "
        f"{len(index.get('manifests', []))} entries"
    )


def fetch_platform_manifest(
    client: RegistryClient,
) -> tuple[dict[str, Any], str]:
    """Fetch the tag manifest and, for indexes, resolve to the platform one.

    Returns ``(manifest, platform_digest)`` where the digest is the cache key
    (index digest is not a stable key for layers: they differ per arch).
    """
    manifest, _digest = client.manifest()
    if manifest.get("mediaType") in _INDEX_MEDIA_TYPES or "manifests" in manifest:
        entry = select_platform_manifest(manifest)
        platform_manifest, platform_digest = client.manifest(entry["digest"])
        return platform_manifest, platform_digest
    return manifest, _digest


def _whiteout_parts(member_name: str) -> tuple[Path, str, str] | None:
    """Return ``(dir_path, marker, target_name)`` for whiteout members.

    ``.wh.<name>`` removes ``<name>`` under the member's directory;
    ``.wh..wh..opq`` clears the whole directory (opaque).
    """
    name = posixpath.normpath(member_name)
    if name in ("", "."):
        return None
    base = posixpath.basename(name)
    directory = posixpath.dirname(name)
    if base == ".wh..wh..opq":
        return Path(directory), "opq", ""
    if base.startswith(".wh."):
        return Path(directory), "wh", base[len(".wh."):]
    return None


def extract_layer(layer_bytes: bytes, dest: Path) -> None:
    """Extract one (possibly gzipped) OCI layer into ``dest`` with whiteouts.

    Whiteout members are applied before the remaining members are extracted:
    ``.wh.<name>`` deletes an entry from a lower layer, ``.wh..wh..opq``
    clears the directory it sits in (opaque). Path traversal and absolute
    member names are rejected; absolute symlink targets are rewritten to
    chroot-relative links (the rootfs is consumed inside a chroot, so an
    absolute target like ``/etc/ssl`` must keep resolving inside it, and
    the ``data`` extraction filter would otherwise drop the member).
    """
    dest = dest.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(layer_bytes), mode="r:*") as tar:
        members = list(tar)
        # 1) Apply whiteouts against the assembled tree.
        for member in members:
            parts = _whiteout_parts(member.name)
            if parts is None:
                continue
            directory, kind, target = parts
            if kind == "opq":
                _remove_contents(_safe_join(dest, directory))
            else:
                _remove_path(_safe_join(dest, directory) / target)
        # 2) Extract the remaining members with path-safety filtering.
        safe_members = [
            m for m in members if _whiteout_parts(m.name) is None
        ]
        safe_members = [m for m in safe_members if _member_is_safe(m, dest)]
        _chroot_symlinks(safe_members)
        try:
            tar.extractall(dest, members=safe_members, filter="data")
        except TypeError:  # pragma: no cover - Python < 3.12
            _extractall_compat(tar, safe_members, dest)


def _safe_join(root: Path, relative: Path) -> Path:
    target = (root / str(relative)).resolve()
    if not target.is_relative_to(root):
        raise RegistryError(f"layer member escapes rootfs: {relative}")
    return target


def _member_is_safe(member: tarfile.TarInfo, dest: Path) -> bool:
    name = posixpath.normpath(member.name)
    if name.startswith("/") or ".." in name.split("/"):
        return False
    return True


def _chroot_symlinks(members: Iterable[tarfile.TarInfo]) -> None:
    """Rewrite absolute symlink targets to chroot-safe relative targets.

    Images ship links such as ``/usr/lib/ssl/cert.pem -> /etc/ssl/certs/
    ca-certificates.crt``. The extracted tree is used as a chroot root, so
    the absolute target must resolve inside the tree; tarfile's ``data``
    filter rejects absolute links outright, and a raw absolute link would
    also escape the rootfs when the tree is read from the host. Converting
    to a relative link keeps the in-chroot semantics identical and the
    link inside the rootfs from the host side too.
    """
    for member in members:
        if not (member.issym() and posixpath.isabs(member.linkname)):
            continue
        target = posixpath.normpath(member.linkname)
        link_dir = posixpath.dirname(posixpath.normpath(member.name)) or "."
        # Relate the target to the chroot root (where the member lives),
        # not to the host cwd: ``posixpath.relpath`` with a relative start
        # would anchor at cwd and produce an escaping ``../../..`` chain.
        member.linkname = posixpath.relpath(
            target, posixpath.normpath(posixpath.join("/", link_dir))
        )


def _extractall_compat(
    tar: tarfile.TarFile, members: Iterable[tarfile.TarInfo], dest: Path
) -> None:  # pragma: no cover - Python < 3.12
    for member in members:
        tar.extract(member, dest)


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir() and not path.is_symlink():
            import shutil

            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass


def _remove_contents(path: Path) -> None:
    import shutil

    if not path.is_dir() or path.is_symlink():
        return
    for child in list(path.iterdir()):
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink(missing_ok=True)
