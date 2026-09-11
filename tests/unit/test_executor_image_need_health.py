"""B1 fix round 2: the image probes share the factory's health judgment.

Both ``envd_service.agent._executor_needs_images`` and the control-plane twin
answer "does this executor resolve image rootfs?". An *unusable* sandlock
package is not a fallback case -- the factory fails closed on it -- so the
probes must raise the same reason instead of answering False and hiding it
behind a missing rootfs. A simply-absent package keeps the quiet False.
"""

from __future__ import annotations

import builtins
import sys
from types import ModuleType

import pytest

import control_plane.api.sandboxes as cp_sandboxes
import envd_service.agent as worker_agent

PROBES = (worker_agent._executor_needs_images, cp_sandboxes._executor_needs_images)
PROBE_IDS = ("worker", "control_plane")


@pytest.fixture(params=PROBES, ids=PROBE_IDS)
def probe(request):
    return request.param


def _fail_sandlock_import(monkeypatch, failure: BaseException) -> None:
    """Make ``import sandlock`` raise `failure` inside the probe."""
    real_import = builtins.__import__

    def _import(name, *args, **kwargs):
        if name == "sandlock":
            raise failure
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _import)


def _expected_unusable(detail: str) -> str:
    """The exact fail-closed text, written out rather than imported."""
    return (
        "E2B_EXECUTOR=auto cannot run: the sandlock package is installed but "
        f"unusable ({detail}); refusing to fall back to the LOCAL executor, "
        "which applies no sandbox confinement. Reinstall the matching sandlock "
        "wheel (or rebuild libsandlock_ffi.so) and restage the worker image."
    )


def test_absent_package_is_false(probe, monkeypatch) -> None:
    """Not installed is the documented auto-mode fallback (no images needed)."""
    _fail_sandlock_import(monkeypatch, ModuleNotFoundError("No module named 'sandlock'"))
    assert probe("auto") is False


def test_unusable_package_raises_the_shared_reason(probe, monkeypatch) -> None:
    """An installed-but-broken package must not read as "no images needed"."""
    _fail_sandlock_import(
        monkeypatch, ImportError("cannot import name 'InstanceClosedError'")
    )

    with pytest.raises(RuntimeError) as info:
        probe("auto")

    assert type(info.value) is RuntimeError
    assert str(info.value) == _expected_unusable(
        "ImportError: cannot import name 'InstanceClosedError'"
    )


def test_missing_reason_export_runtime_error_also_fails_closed(
    probe, monkeypatch
) -> None:
    """The SDK's own named RuntimeError is exactly the case to catch."""
    sdk_error = RuntimeError(
        "sandlock_create_with_err is missing from the loaded sandlock library "
        "(/usr/lib/libsandlock_ffi.so). This Python package requires the "
        "create/launch exports that carry a failure reason; without them a "
        "sandbox failure would be reported without its cause, so this is "
        "refused rather than silently degraded."
    )
    _fail_sandlock_import(monkeypatch, sdk_error)

    with pytest.raises(RuntimeError) as info:
        probe("auto")

    assert type(info.value) is RuntimeError
    message = str(info.value)
    assert message.startswith("E2B_EXECUTOR=auto cannot run: ")
    assert "sandlock_create_with_err is missing from the loaded sandlock library" in message
    assert "applies no sandbox confinement" in message


def test_abi_probe_failure_also_raises(probe, monkeypatch) -> None:
    """A healthy import whose ABI call fails is broken too, not "False"."""
    stub = ModuleType("sandlock")

    def _boom() -> int:
        raise AttributeError("landlock_abi_version")

    stub.landlock_abi_version = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sandlock", stub)

    with pytest.raises(RuntimeError) as info:
        probe("auto")

    assert type(info.value) is RuntimeError
    assert str(info.value) == _expected_unusable(
        "AttributeError: landlock_abi_version"
    )


@pytest.mark.parametrize("mode,expected", [("local", False), ("sandlock", True)])
def test_explicit_modes_keep_their_answers(probe, mode, expected) -> None:
    assert probe(mode) is expected
