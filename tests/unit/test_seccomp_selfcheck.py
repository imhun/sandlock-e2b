"""The worker refuses to run unfiltered, and refuses a filter that is not ours.

Two silent failure modes motivate this (docs/production-deployment-requirements.md
§2.4.3): a worker running without any seccomp filter hands every sandbox its own
syscall surface, and a *default* filter instead of the shipped profile is what a
k8s node with a missing Localhost profile looks like -- the kubelet skips the
missing file and the pod comes up anyway (kubernetes#124944), so sandbox creates
fail later with nothing pointing at the profile.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from envd_service.config import (
    SECCOMP_FILTER_MISSING_ERROR,
    SECCOMP_PROFILE_NOT_APPLIED_ERROR,
    check_seccomp_filter,
    parse_seccomp_mode,
    read_seccomp_mode,
)

REQUIRED = SimpleNamespace(require_seccomp_filter=True)
OPTIONAL = SimpleNamespace(require_seccomp_filter=False)

STATUS_FILTERED = "Name:\tpython\nSeccomp:\t2\nSeccomp_filters:\t1\n"
STATUS_DISABLED = "Name:\tpython\nSeccomp:\t0\n"
STATUS_STRICT = "Name:\tpython\nSeccomp:\t1\n"
STATUS_NO_FIELD = "Name:\tpython\nState:\tS (sleeping)\n"


def test_parse_reads_the_seccomp_field() -> None:
    assert parse_seccomp_mode(STATUS_FILTERED) == 2
    assert parse_seccomp_mode(STATUS_DISABLED) == 0
    assert parse_seccomp_mode(STATUS_STRICT) == 1
    assert parse_seccomp_mode(STATUS_NO_FIELD) is None


def test_read_returns_none_without_proc(tmp_path) -> None:
    assert read_seccomp_mode(str(tmp_path / "missing")) is None


def test_off_linux_skips_the_check() -> None:
    """macOS dev hosts have no /proc: nothing to assert, no crash."""
    assert check_seccomp_filter(REQUIRED, status_text=STATUS_NO_FIELD) is None


@pytest.mark.parametrize("mode_text", [STATUS_DISABLED, STATUS_STRICT])
def test_unfiltered_worker_is_refused(mode_text: str) -> None:
    with pytest.raises(RuntimeError) as excinfo:
        check_seccomp_filter(REQUIRED, status_text=mode_text)
    assert str(excinfo.value) == SECCOMP_FILTER_MISSING_ERROR.format(
        mode=parse_seccomp_mode(mode_text),
        meaning="filtering disabled"
        if parse_seccomp_mode(mode_text) == 0
        else "strict mode, not a filter",
    )


def test_unfiltered_worker_is_allowed_when_explicitly_opted_out() -> None:
    assert check_seccomp_filter(OPTIONAL, status_text=STATUS_DISABLED) == 0


def test_filtered_worker_with_working_userns_passes() -> None:
    assert (
        check_seccomp_filter(
            REQUIRED, status_text=STATUS_FILTERED, probe_runner=lambda: (True, "")
        )
        == 2
    )


def test_default_filter_instead_of_ours_is_refused(monkeypatch) -> None:
    """A filter is present but `unshare` is still gated -> not the shipped one."""
    monkeypatch.setattr(
        "envd_service.config._userns_limited_by_host", lambda: None
    )
    with pytest.raises(RuntimeError) as excinfo:
        check_seccomp_filter(
            REQUIRED,
            status_text=STATUS_FILTERED,
            probe_runner=lambda: (False, "OSError: [Errno 1] Operation not permitted"),
        )
    assert "SECCOMP_PROFILE_NOT_APPLIED" in str(excinfo.value)
    assert "Errno 1" in str(excinfo.value)


def test_default_filter_with_a_host_restriction_is_a_warning(monkeypatch) -> None:
    """Unprivileged userns switched off for the whole host is not our profile's fault."""
    monkeypatch.setattr(
        "envd_service.config._userns_limited_by_host",
        lambda: "the host has unprivileged user namespaces disabled "
        "(user.max_user_namespaces=0)",
    )
    assert (
        check_seccomp_filter(
            REQUIRED,
            status_text=STATUS_FILTERED,
            probe_runner=lambda: (False, "OSError: [Errno 1] Operation not permitted"),
        )
        == 2
    )


def test_opt_out_downgrades_the_profile_mismatch(monkeypatch) -> None:
    monkeypatch.setattr("envd_service.config._userns_limited_by_host", lambda: None)
    assert (
        check_seccomp_filter(
            OPTIONAL,
            status_text=STATUS_FILTERED,
            probe_runner=lambda: (False, "OSError: [Errno 1] Operation not permitted"),
        )
        == 2
    )
    assert SECCOMP_PROFILE_NOT_APPLIED_ERROR.startswith("SECCOMP_PROFILE_NOT_APPLIED")


def test_create_app_runs_the_selfcheck(monkeypatch, tmp_path) -> None:
    """The guard is wired into the app factory, not just available."""
    import envd_service.app as app_mod
    from envd_service.config import Settings

    called = {}

    def fake_check(settings, **kwargs):  # noqa: ANN001, ANN003
        called["settings"] = settings

    monkeypatch.setattr(app_mod, "check_seccomp_filter", fake_check)
    app_mod.create_app(
        settings=Settings(require_seccomp_filter=False),
        workspace_base=tmp_path,
    )
    assert "settings" in called
