"""W6: ``GET /files`` on a sandbox-private (``0600``/``0700``) entry.

The deployed worker reaches managed entries through its **group** identity, so
an entry the sandbox itself restricted to ``0600`` (or a file inside a ``0700``
parent directory) is out of its reach. That is a permanent property of the
deployment shape, not a worker fault, so the API has to name it:

* before W6 the read answered ``500`` with the raw ``[Errno 13] Permission
  denied`` text, which the SDK reports as
  ``SandboxException("500: ...")`` — measured on the non-root stack in
  ``tmp/f1/f1-c1-locked-file-probe.log`` — looking like a broken platform,
  with nothing the caller can act on;
* now a denied read (or a denied ``stat`` under a ``0700`` parent) answers
  ``403`` with a reason the client can act on.

The kernel denial is emulated at the ``pathlib`` boundary because a root test
runner bypasses real mode bits (the same convention as
``tests/unit/test_quota_agent_server.py``); the non-root F1 probe
(``tmp/f1/f1-c1-locked-file-probe.log``) is where the real mode bits were
measured.
"""

from __future__ import annotations

import errno
from pathlib import Path

#: The client-visible reason, spelled out here on purpose: this is the
#: contract the SDK's ``SandboxException("403: ...")`` carries to the caller.
PRIVATE_REASON = (
    "the sandbox made this entry private (0600/0700, or a 0700 parent "
    "directory): the worker reads sandbox files with its group identity, so "
    "the entry is out of its reach; read it from inside the sandbox or relax "
    "that entry's mode"
)


async def _create_sandbox(control_client) -> dict:
    response = await control_client.post(
        "/sandboxes",
        headers={"X-API-Key": "local-key"},
        json={"templateID": "base", "timeout": 300},
    )
    assert response.status_code == 201
    return response.json()


def _headers(sandbox: dict) -> dict:
    return {
        "E2b-Sandbox-Id": sandbox["sandboxID"],
        "X-Access-Token": sandbox["envdAccessToken"],
    }


async def _upload_locked(envd_client, sandbox: dict, apps) -> Path:
    """Write ``workspace/locked.txt`` and return its host path."""
    _, envd_app = apps
    uploaded = await envd_client.post(
        "/files",
        headers={**_headers(sandbox), "Content-Type": "application/octet-stream"},
        params={"path": "workspace/locked.txt"},
        content=b"secret\n",
    )
    assert uploaded.status_code == 200
    runtime = envd_app.state.runtime_registry.get(sandbox["sandboxID"])
    return Path(runtime.workspace_dir) / "workspace" / "locked.txt"


def _deny(monkeypatch, name: str, target: Path) -> None:
    """Make one ``pathlib`` operation EACCES for ``target`` only."""
    real = getattr(Path, name)

    def denied(self, *args, **kwargs):
        if self == target:
            raise PermissionError(errno.EACCES, "Permission denied", str(self))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, name, denied)


def _denied_response(target: Path) -> dict:
    return {
        "message": "Path workspace/locked.txt is not readable: "
        f"{PRIVATE_REASON} ([Errno 13] Permission denied: '{target}')"
    }


async def test_a_private_file_read_is_forbidden(
    apps, control_client, envd_client, monkeypatch
) -> None:
    sandbox = await _create_sandbox(control_client)
    target = await _upload_locked(envd_client, sandbox, apps)
    _deny(monkeypatch, "read_bytes", target)

    response = await envd_client.get(
        "/files", headers=_headers(sandbox), params={"path": "workspace/locked.txt"}
    )

    assert response.status_code == 403
    assert response.json() == _denied_response(target)


async def test_a_file_under_a_private_directory_read_is_forbidden(
    apps, control_client, envd_client, monkeypatch
) -> None:
    """A 0700 parent directory must not collapse into "not found"."""
    sandbox = await _create_sandbox(control_client)
    target = await _upload_locked(envd_client, sandbox, apps)
    _deny(monkeypatch, "stat", target)

    response = await envd_client.get(
        "/files", headers=_headers(sandbox), params={"path": "workspace/locked.txt"}
    )

    assert response.status_code == 403
    assert response.json() == _denied_response(target)
