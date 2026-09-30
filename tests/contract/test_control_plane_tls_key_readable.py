"""Contract lane for review item 2: the TLS pair must be readable *by uid 65534*.

The unit pin (`tests/unit/test_control_plane_tls_recipe.py`) asserts the mode the
generator leaves behind. This lane asserts the property that mode exists for, on
a real container, with the ownership a **native Linux Docker host** gives a bind
mounted pair: the control plane reads `/tls/tls.crt` + `/tls/tls.key` as uid
65534, and the call it makes is `ssl.SSLContext.load_cert_chain` -- the same one
uvicorn runs at startup (`uvicorn.config.create_ssl_context`), which is where the
regression died:

    PermissionError: [Errno 13] Permission denied

The pair is staged inside a **named volume** rather than mounted from the host on
purpose: OrbStack presents a host bind mount as owned by *the container's own
user* (a root container sees `0:0`, a 65534 container sees `65534:65534`), so a
`0600` host key is readable here and the failure mode cannot be reproduced from a
macOS bind mount at all. Inside a volume the owner is whoever wrote it -- root,
in the arm below -- which is what the target host does.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
GEN = REPO / "deploy" / "scripts" / "gen-tls-cert.sh"

#: The same base the other docker contract lanes use; only the stdlib `ssl`
#: module is needed for the call under test.
IMAGE = "python:3.14-slim"
STAGER = "alpine:latest"

#: The control plane's uid in every lane (k8s `runAsUser`, compose `user:`).
CP_UID = "65534:65534"

LOAD = (
    "import ssl; ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER).load_cert_chain("
    "'/tls/tls.crt', '/tls/tls.key')"
)


def _run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        list(args), capture_output=True, text=True, check=False, timeout=180
    )
    if check and result.returncode != 0:
        raise AssertionError(f"command failed: {args}\n{result.stderr}")
    return result


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return _run("docker", "info", check=False).returncode == 0


pytestmark = pytest.mark.skipif(
    not _docker_ready(),
    reason="needs a Docker daemon (this lane stages a pair in a volume and "
    "reads it as uid 65534, the way the control plane does)",
)


@pytest.fixture()
def staged_pair(tmp_path: Path):
    """The recipe's own output, staged root-owned inside a named volume."""
    out = tmp_path / "tls"
    _run(str(GEN), str(out), "control-plane")
    volume = f"e2b-tls-contract-{uuid.uuid4().hex[:12]}"
    _run("docker", "volume", "create", volume)
    try:
        _run(
            "docker", "run", "--rm",
            "-v", f"{volume}:/tls",
            "-v", f"{out}:/src:ro",
            "--entrypoint", "sh", STAGER, "-c",
            # root:root, the shape a native Linux bind mount / Secret has.
            "cp /src/tls.crt /src/tls.key /tls/ && chown root:root /tls/tls.crt "
            "/tls/tls.key && chmod 644 /tls/tls.crt && chmod 600 /tls/tls.key",
        )
        yield volume, out
    finally:
        _run("docker", "volume", "rm", "-f", volume, check=False)


def _read_key(volume: str, *, user: str = CP_UID) -> subprocess.CompletedProcess:
    return _run(
        "docker", "run", "--rm",
        "-u", user,
        "-v", f"{volume}:/tls:ro",
        "--entrypoint", "python3", IMAGE, "-c", LOAD,
        check=False,
    )


def test_a_0600_pair_owned_by_someone_else_is_unreadable_to_the_cp_uid(
    staged_pair,
) -> None:
    """The counter-arm: this is the regression, reproduced."""
    volume, _out = staged_pair
    result = _read_key(volume)

    assert result.returncode != 0
    assert "PermissionError: [Errno 13] Permission denied" in result.stderr


def test_the_recipes_own_mode_is_readable_to_the_cp_uid(staged_pair) -> None:
    """The positive arm: the recipe's mode, read by the uid the CP runs as.

    Staged over the counter-arm's 0600 file with the mode the generator writes,
    so the two arms differ by exactly the thing under test.
    """
    volume, out = staged_pair
    assert stat.S_IMODE((out / "tls.key").stat().st_mode) == 0o644
    _run(
        "docker", "run", "--rm",
        "-v", f"{volume}:/tls",
        "--entrypoint", "chmod", STAGER, "644", "/tls/tls.key",
    )

    result = _read_key(volume)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
