"""A registry fixture whose setup fails must *fail*, never skip.

``image_registry_url`` and ``authenticated_registry`` used to call
``pytest.skip`` on every setup step that did not come good (the container
refused to start, no port was published, it never answered ``/v2/``), which
silently dropped the registry-backed template-push contracts -- the same shape
the buildkitd fixture had before ``a2bb451``.

Only the docker-capability check in front of them (no ``docker`` binary on the
machine at all -- also in ``_STRICT_SKIP_FORBIDDEN``, so inside the gate it is
a failure too) is still a skip; every step after it raises with the registry's
own output. That is what these tests pin, by putting a stub ``docker`` on
``PATH`` that fails the fixture's first call and checking the exact message.
"""

from __future__ import annotations

import os

import pytest

import tests.conftest as conftest

_STUB_STDERR = "stub docker: the daemon is not answering"


def _stub_docker(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``docker`` on ``PATH`` whose every call fails loudly on stderr."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "docker"
    stub.write_text(f"#!/bin/sh\necho '{_STUB_STDERR}' >&2\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        # The plain registry fails on ``docker run registry:2`` ...
        (
            conftest.image_registry_url,
            f"cannot start registry container: {_STUB_STDERR}",
        ),
        # ... and the authenticated one on the htpasswd helper it runs first.
        (
            conftest.authenticated_registry,
            f"cannot generate htpasswd: {_STUB_STDERR}",
        ),
    ],
    ids=["image_registry_url", "authenticated_registry"],
)
def test_a_failing_registry_setup_is_a_failure_not_a_skip(
    fixture, expected: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _stub_docker(tmp_path, monkeypatch)
    with pytest.raises(pytest.fail.Exception) as excinfo:
        list(fixture.__wrapped__())
    assert str(excinfo.value) == expected


@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        (
            conftest.image_registry_url,
            "docker is required for the image registry tests",
        ),
        (
            conftest.authenticated_registry,
            "docker is required for the authenticated registry tests",
        ),
    ],
    ids=["image_registry_url", "authenticated_registry"],
)
def test_the_only_registry_skip_left_is_the_docker_capability(
    fixture, reason: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """No docker binary at all -> the one honest skip, and it is audited.

    ``_STRICT_SKIP_FORBIDDEN`` carries the same marker, so a gate run on a
    runner without the daemon socket is a failure rather than a quieter run.
    """
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(pytest.skip.Exception) as excinfo:
        list(fixture.__wrapped__())
    assert str(excinfo.value) == reason
    assert reason in conftest._STRICT_SKIP_FORBIDDEN
