"""E2B_PURE_ROOTFS / E2B_PURE_ROOTFS_DIR: the shape switch and its landing spot."""
from __future__ import annotations

from pathlib import Path

from gateway_common.paths import PURE_ROOTFS_DIR_NAME, is_reserved_platform_namespace
from envd_service.config import Settings


def test_the_switch_defaults_to_off(monkeypatch) -> None:
    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    assert Settings().pure_rootfs == "off"


def test_the_switch_is_normalised(monkeypatch) -> None:
    monkeypatch.setenv("E2B_PURE_ROOTFS", " SYNTH ")
    assert Settings().pure_rootfs == "synth"


def test_the_root_dir_defaults_beside_the_sandbox_trees(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("E2B_PURE_ROOTFS_DIR", raising=False)
    monkeypatch.setenv("E2B_WORKSPACE_BASE", str(tmp_path / "base"))
    assert Settings().pure_rootfs_dir == (tmp_path / "base").resolve() / PURE_ROOTFS_DIR_NAME


def test_the_root_dir_can_be_pinned(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("E2B_PURE_ROOTFS_DIR", str(tmp_path / "elsewhere"))
    assert Settings().pure_rootfs_dir == (tmp_path / "elsewhere").resolve()


def test_the_namespace_is_reserved() -> None:
    assert PURE_ROOTFS_DIR_NAME == "_pure_rootfs"
    assert is_reserved_platform_namespace(PURE_ROOTFS_DIR_NAME) is True


def test_a_reserved_shaped_name_is_not_a_sandbox_tree(tmp_path) -> None:
    """The reserved list is a statement about names, not about records."""
    from gateway_common.paths import is_sandbox_workspace_dir

    tree = tmp_path / "sbx_keep"
    tree.mkdir()
    assert is_sandbox_workspace_dir(tree) is True
    assert is_sandbox_workspace_dir(tmp_path / PURE_ROOTFS_DIR_NAME) is False


def _factory(monkeypatch, workspace: Path, *, switch: str | None):
    """``create_executor`` on a stubbed-healthy sandlock host, pure shape.

    The sandlock probes are stubbed because this dev host is macOS (no
    sandlock wheel): what is under test is the switch's wiring into
    :class:`SandlockExecutor`, which is the same code path on either host.
    """
    import envd_service.executors.factory as factory_mod
    from envd_service.executors.factory import create_executor

    monkeypatch.setattr(factory_mod, "_import_sandlock", lambda: None)
    monkeypatch.setattr(factory_mod, "_sandlock_available", lambda: True)
    monkeypatch.setattr(factory_mod, "_landlock_ok", lambda: True)
    if switch is None:
        monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    else:
        monkeypatch.setenv("E2B_PURE_ROOTFS", switch)
    settings = Settings(executor="sandlock", workspace_base=workspace)
    executor = create_executor(
        settings,
        workspace_dir=str(workspace / "sbx_switch"),
        base_image=None,
        memory_mb=512,
        cpu_percent=100,
        disk_mb=1024,
        max_processes=64,
        max_open_files=4096,
        allow_internet_access=False,
        network=None,
        sandbox_id="sbx_switch",
    )
    return settings, executor


def test_the_factory_hands_over_no_root_while_the_switch_is_off(
    monkeypatch, tmp_path
) -> None:
    """Default off: the pure shape keeps N15's identity translation."""
    settings, executor = _factory(monkeypatch, tmp_path, switch=None)

    assert settings.pure_rootfs == "off"
    assert executor._pure_rootfs_dir is None
    assert executor._synthetic_rootfs is None
    assert executor._has_sandbox_root is False
    assert executor._chroot_root == "/"


def test_the_factory_hands_over_the_root_once_the_switch_is_synth(
    monkeypatch, tmp_path
) -> None:
    """``synth``: the configured directory, per sandbox, under ``<id>``."""
    settings, executor = _factory(monkeypatch, tmp_path, switch="synth")

    assert settings.pure_rootfs == "synth"
    assert executor._pure_rootfs_dir == settings.pure_rootfs_dir
    assert executor._synthetic_rootfs == settings.pure_rootfs_dir / "sbx_switch"
    assert executor._has_sandbox_root is True


def test_the_security_helper_mirrors_the_shape_switch(monkeypatch, tmp_path) -> None:
    """A lane that sets the env must reach the executor the tests build.

    `route_b_sandbox` is the only place the security suite builds a shape, so a
    switch it does not read is a lane that silently tests the old shape.
    """
    from tests.security.conftest import route_b_sandbox

    monkeypatch.setenv("E2B_PURE_ROOTFS", "synth")
    monkeypatch.setenv("E2B_PURE_ROOTFS_DIR", str(tmp_path / "_pure_rootfs"))
    executor, _workspace = route_b_sandbox(None, None)
    try:
        assert executor._has_sandbox_root is True
        assert executor._chroot_root.startswith(str(tmp_path / "_pure_rootfs"))
    finally:
        executor.close()


def test_the_security_helper_stays_on_the_identity_root_by_default(
    monkeypatch, tmp_path
) -> None:
    from tests.security.conftest import route_b_sandbox

    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    executor, _workspace = route_b_sandbox(None, None)
    try:
        assert executor._has_sandbox_root is False
        assert executor._chroot_root == "/"
    finally:
        executor.close()


def test_the_synthetic_root_is_refused_without_the_real_root() -> None:
    """`E2B_PURE_ROOTFS=synth` + `E2B_REAL_ROOT` off cannot work (2026-09-26).

    The synthesized root is an empty skeleton, and only the real root (the
    mount namespace + `pivot_root` path) binds the host system directories, the
    workspace and the volumes into it. With the emulated root every path stays
    inside that skeleton, so the sandbox's own `/bin/sh` does not exist: the
    create dies with errno 13 and every later command answers `instance is
    closed`. The guard names the two switches and the way out, so a
    misconfigured worker refuses to start instead of serving sandboxes that are
    dead on arrival.
    """
    from types import SimpleNamespace

    import pytest

    from envd_service.app import create_app
    from envd_service.config import (
        PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR,
        check_pure_rootfs_pairing,
    )

    def _settings(pure_rootfs: str, real_root: bool) -> SimpleNamespace:
        return SimpleNamespace(pure_rootfs=pure_rootfs, real_root=real_root)

    # The refused shape, and its exact message.
    with pytest.raises(RuntimeError) as excinfo:
        check_pure_rootfs_pairing(_settings("synth", False))
    assert str(excinfo.value) == PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR

    # The user's own list of switches stays reachable: off, or paired.
    assert check_pure_rootfs_pairing(_settings("off", False)) is None
    assert check_pure_rootfs_pairing(_settings("off", True)) is None
    assert check_pure_rootfs_pairing(_settings("synth", True)) is None

    # The worker refuses to come up with that same sentence, which is the path
    # a deployment actually takes.
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings=_settings("synth", False))
    assert str(excinfo.value) == PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR
