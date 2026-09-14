"""Control-plane API TLS smoke tests (E1.4).

Covers the HTTPS shape of the control plane API (``uvicorn ssl_certfile``):
with ``E2B_TLS_CERT``/``E2B_TLS_KEY`` configured the server speaks TLS and a
client that skips verification (self-signed local cert) gets ``200`` on the
health endpoints; a plain HTTP request against the TLS port fails the TLS
handshake (no redirect, no silent downgrade). Without TLS configured the
server stays plain HTTP (zero-regression guard).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from control_plane.app import create_app
from control_plane.config import Settings, uvicorn_ssl_kwargs


def _gen_self_signed_cert(tmp_path: Path) -> tuple[str, str]:
    """Self-signed cert/key (SAN localhost + 127.0.0.1 + ::1) via openssl."""
    if shutil.which("openssl") is None:
        pytest.skip("openssl CLI required to generate the test certificate")
    cert = tmp_path / "tls.crt"
    key = tmp_path / "tls.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1",
            "-addext",
            "extendedKeyUsage=serverAuth",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return str(cert), str(key)


@pytest.fixture()
def tls_server(tmp_path):
    """Real uvicorn HTTPS control plane (self-signed cert, ephemeral port)."""
    from tests.conftest import _ServerThread, _bind_low_port

    cert, key = _gen_self_signed_cert(tmp_path)
    port, sock = _bind_low_port()
    app = create_app(settings=Settings(api_keys=("local-key",)))
    server = _ServerThread(
        app, port, sock=sock, ssl_certfile=cert, ssl_keyfile=key
    )
    server.start()
    yield port
    server.stop()


@pytest.fixture()
def http_server(tmp_path):
    """Plain-HTTP control plane (E1.4 zero-regression guard)."""
    from tests.conftest import _ServerThread, _bind_low_port

    port, sock = _bind_low_port()
    app = create_app(settings=Settings(api_keys=("local-key",)))
    server = _ServerThread(app, port, sock=sock)
    server.start()
    yield port
    server.stop()


@pytest.mark.asyncio
async def test_https_health_200(tls_server):
    """curl -k equivalent: https health endpoints answer 200 over TLS."""
    port = tls_server
    async with httpx.AsyncClient(verify=False) as client:
        root = await client.get(f"https://127.0.0.1:{port}/")
        assert root.status_code == 200
        assert root.json() == {"status": "ok", "service": "e2b-sandlock"}

        healthz = await client.get(f"https://127.0.0.1:{port}/healthz")
        assert healthz.status_code == 200
        assert healthz.json() == {"status": "ok"}

        # SAN covers localhost too (the deploy/scripts/gen-tls-cert.sh shape).
        localhost = await client.get(f"https://localhost:{port}/healthz")
        assert localhost.status_code == 200
        assert localhost.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_plain_http_against_tls_port_fails(tls_server):
    """No silent downgrade: HTTP on the TLS port fails the TLS handshake."""
    port = tls_server
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.HTTPError):
            await client.get(f"http://127.0.0.1:{port}/healthz")
    # The TLS listener is untouched by the failed plaintext attempt.
    async with httpx.AsyncClient(verify=False) as client:
        healthz = await client.get(f"https://127.0.0.1:{port}/healthz")
        assert healthz.status_code == 200


@pytest.mark.asyncio
async def test_http_control_plane_unchanged_without_tls(http_server):
    """Zero regression: no TLS configured => plain HTTP, same health shape."""
    port = http_server
    async with httpx.AsyncClient() as client:
        healthz = await client.get(f"http://127.0.0.1:{port}/healthz")
        assert healthz.status_code == 200
        assert healthz.json() == {"status": "ok"}


def test_tls_pairing_validation():
    """Half-configured TLS is a hard error, not a silent HTTP fallback."""
    with pytest.raises(ValueError, match="E2B_TLS_CERT and E2B_TLS_KEY"):
        uvicorn_ssl_kwargs(Settings(tls_cert_file="/tls/tls.crt"))
    with pytest.raises(ValueError, match="E2B_TLS_CERT and E2B_TLS_KEY"):
        uvicorn_ssl_kwargs(Settings(tls_key_file="/tls/tls.key"))
    assert uvicorn_ssl_kwargs(Settings()) == {}
    assert uvicorn_ssl_kwargs(
        Settings(tls_cert_file="/tls/tls.crt", tls_key_file="/tls/tls.key")
    ) == {"ssl_certfile": "/tls/tls.crt", "ssl_keyfile": "/tls/tls.key"}


def test_tls_enabled_property():
    assert Settings().tls_enabled is False
    assert Settings(tls_cert_file="/tls/tls.crt").tls_enabled is False
    assert (
        Settings(tls_cert_file="/tls/tls.crt", tls_key_file="/tls/tls.key").tls_enabled
        is True
    )
