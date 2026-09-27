"""E2B_PURE_ROOTFS / E2B_PURE_ROOTFS_DIR: the shape switch and its landing spot.

Since the 2026-09-27 ruling the pure shape's *default* root is the synthesized
one (N16), not N15's identity translation: the residual N27 measured on the
default shape -- `<export>` listing the names of `state`/`_secrets` -- only
exists while the sandbox has no root of its own. The pair `E2B_PURE_ROOTFS`
+ `E2B_REAL_ROOT` therefore travels together by default, and the one key back
to the old shape is `E2B_PURE_ROOTFS=off`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from gateway_common.paths import PURE_ROOTFS_DIR_NAME, is_reserved_platform_namespace
from envd_service.config import Settings, resolve_real_root


def test_the_switch_defaults_to_synth(monkeypatch) -> None:
    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    assert Settings().pure_rootfs == "synth"


def test_the_retreat_lever_is_off(monkeypatch) -> None:
    """`off` is the one key that puts the pure shape back on N15's identity root."""
    monkeypatch.setenv("E2B_PURE_ROOTFS", "off")
    assert Settings().pure_rootfs == "off"


def test_an_empty_value_is_the_default_not_the_lever(monkeypatch) -> None:
    """Empty stays "unset" (the convention of the other env helpers)."""
    monkeypatch.setenv("E2B_PURE_ROOTFS", "")
    assert Settings().pure_rootfs == "synth"


def test_the_switch_is_normalised(monkeypatch) -> None:
    monkeypatch.setenv("E2B_PURE_ROOTFS", " SYNTH ")
    assert Settings().pure_rootfs == "synth"


def test_the_real_root_is_unset_by_default(monkeypatch) -> None:
    """`E2B_REAL_ROOT` is a tri-state: unset is not `off` (see the pairing)."""
    monkeypatch.delenv("E2B_REAL_ROOT", raising=False)
    assert Settings().real_root is None


def test_the_real_root_can_be_named_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("E2B_REAL_ROOT", "0")
    assert Settings().real_root is False
    monkeypatch.setenv("E2B_REAL_ROOT", "1")
    assert Settings().real_root is True


def test_the_coupled_default_follows_the_synthesized_root(monkeypatch) -> None:
    monkeypatch.delenv("E2B_REAL_ROOT", raising=False)
    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    settings = Settings()
    assert resolve_real_root(settings, pure_shape=True) is True
    assert resolve_real_root(settings, pure_shape=False) is False


def test_the_coupled_default_does_not_survive_the_retreat_lever(monkeypatch) -> None:
    """`E2B_PURE_ROOTFS=off` is the whole retreat, not half of it."""
    monkeypatch.delenv("E2B_REAL_ROOT", raising=False)
    monkeypatch.setenv("E2B_PURE_ROOTFS", "off")
    settings = Settings()
    assert resolve_real_root(settings, pure_shape=True) is False
    assert resolve_real_root(settings, pure_shape=False) is False


def test_an_explicit_real_root_outranks_the_coupling(monkeypatch) -> None:
    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    monkeypatch.setenv("E2B_REAL_ROOT", "0")
    assert resolve_real_root(Settings(), pure_shape=True) is False
    monkeypatch.setenv("E2B_REAL_ROOT", "1")
    assert resolve_real_root(Settings(), pure_shape=True) is True


def test_an_explicit_real_root_still_reaches_the_image_shape(monkeypatch) -> None:
    """The coupling is not the switch: `E2B_REAL_ROOT=1` keeps its old meaning."""
    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    monkeypatch.setenv("E2B_REAL_ROOT", "1")
    assert resolve_real_root(Settings(), pure_shape=False) is True


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


def _stub_probe(monkeypatch, *, capability: str = "") -> list[str]:
    """Stand-in for the mount-family probe, recording that it was consulted.

    This dev host is macOS (no sandlock wheel, no ``libc.so.6``), so the real
    probe could only answer "this node cannot build a root" -- what is under
    test here is *when* the executor asks, and that path is the same on either
    host.
    """
    import envd_service.executors.sandlock as sandlock_mod

    calls: list[str] = []

    def _probe() -> str:
        calls.append("probed")
        return capability

    monkeypatch.setattr(sandlock_mod, "_real_root_capability", _probe)
    return calls


def _factory(
    monkeypatch,
    workspace: Path,
    *,
    switch: str | None,
    real_root: str | None = None,
    base_image: str | None = None,
    rootfs: Path | None = None,
):
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
    if rootfs is not None:
        monkeypatch.setattr(factory_mod, "resolve_image_rootfs", lambda *a, **k: rootfs)
    if switch is None:
        monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    else:
        monkeypatch.setenv("E2B_PURE_ROOTFS", switch)
    if real_root is None:
        monkeypatch.delenv("E2B_REAL_ROOT", raising=False)
    else:
        monkeypatch.setenv("E2B_REAL_ROOT", real_root)
    settings = Settings(executor="sandlock", workspace_base=workspace)
    executor = create_executor(
        settings,
        workspace_dir=str(workspace / "sbx_switch"),
        base_image=base_image,
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


def test_the_factory_puts_the_pure_shape_on_its_own_root_by_default(
    monkeypatch, tmp_path
) -> None:
    """The default pair: the synthesized skeleton *and* the root that fills it."""
    calls = _stub_probe(monkeypatch)
    settings, executor = _factory(monkeypatch, tmp_path, switch=None)

    assert settings.pure_rootfs == "synth"
    assert settings.real_root is None
    assert executor._pure_rootfs_dir == settings.pure_rootfs_dir
    assert executor._synthetic_rootfs == settings.pure_rootfs_dir / "sbx_switch"
    assert executor._has_sandbox_root is True
    assert executor._chroot_root == str(settings.pure_rootfs_dir / "sbx_switch")
    assert executor._real_root is True
    assert calls == ["probed"]


def test_the_factory_hands_over_no_root_when_the_lever_is_off(
    monkeypatch, tmp_path
) -> None:
    """`E2B_PURE_ROOTFS=off`: N15's identity translation, and no probe at all."""
    calls = _stub_probe(monkeypatch)
    settings, executor = _factory(monkeypatch, tmp_path, switch="off")

    assert settings.pure_rootfs == "off"
    assert executor._pure_rootfs_dir is None
    assert executor._synthetic_rootfs is None
    assert executor._has_sandbox_root is False
    assert executor._chroot_root == "/"
    assert executor._real_root is False
    assert calls == []


def test_the_factory_hands_over_the_root_once_the_switch_is_synth(
    monkeypatch, tmp_path
) -> None:
    """``synth``: the configured directory, per sandbox, under ``<id>``."""
    calls = _stub_probe(monkeypatch)
    settings, executor = _factory(monkeypatch, tmp_path, switch="synth")

    assert settings.pure_rootfs == "synth"
    assert executor._pure_rootfs_dir == settings.pure_rootfs_dir
    assert executor._synthetic_rootfs == settings.pure_rootfs_dir / "sbx_switch"
    assert executor._has_sandbox_root is True
    assert executor._real_root is True
    assert calls == ["probed"]


def test_the_factory_arms_the_real_root_for_an_explicit_pair(
    monkeypatch, tmp_path
) -> None:
    calls = _stub_probe(monkeypatch)
    _settings, executor = _factory(
        monkeypatch, tmp_path, switch="synth", real_root="1"
    )
    assert executor._real_root is True
    assert calls == ["probed"]


def test_the_coupled_default_leaves_the_image_shape_where_it_was(
    monkeypatch, tmp_path
) -> None:
    """A sandbox with an image never had the pure switch, and does not get it.

    `E2B_BASE_IMAGE` is set by both production manifests, so the fleet's shape
    (and the emulated root a lane asks for with `E2B_REAL_ROOT=0`) has to stay
    exactly where it is: the coupling is the *synthesized* root's, not the
    flag's, and an image sandbox has no synthesized root.
    """
    calls = _stub_probe(monkeypatch)
    rootfs = tmp_path / "image-rootfs"
    rootfs.mkdir()
    settings, executor = _factory(
        monkeypatch,
        tmp_path,
        switch=None,
        base_image="python:3.11-slim",
        rootfs=rootfs,
    )

    assert settings.pure_rootfs == "synth"
    assert executor._synthetic_rootfs is None
    assert executor._has_sandbox_root is True
    assert executor._chroot_root == str(rootfs)
    assert executor._real_root is False
    assert calls == []


def test_the_security_helper_mirrors_the_shape_switch(monkeypatch, tmp_path) -> None:
    """A lane that sets the env must reach the executor the tests build.

    `route_b_sandbox` is the only place the security suite builds a shape, so a
    switch it does not read is a lane that silently tests the old shape.
    """
    from tests.security.conftest import route_b_sandbox

    calls = _stub_probe(monkeypatch)
    monkeypatch.setenv("E2B_PURE_ROOTFS", "synth")
    monkeypatch.setenv("E2B_PURE_ROOTFS_DIR", str(tmp_path / "_pure_rootfs"))
    monkeypatch.delenv("E2B_REAL_ROOT", raising=False)
    executor, _workspace = route_b_sandbox(None, None)
    try:
        assert executor._has_sandbox_root is True
        assert executor._synthetic_rootfs.parent == tmp_path / "_pure_rootfs"
        assert executor._chroot_root == str(executor._synthetic_rootfs)
        assert executor._real_root is True
        assert calls == ["probed"]
    finally:
        executor.close()


def test_the_security_helper_defaults_to_the_synthesized_root(
    monkeypatch, tmp_path
) -> None:
    from tests.security.conftest import route_b_sandbox

    calls = _stub_probe(monkeypatch)
    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    monkeypatch.delenv("E2B_REAL_ROOT", raising=False)
    executor, _workspace = route_b_sandbox(None, None)
    try:
        assert executor._has_sandbox_root is True
        assert executor._real_root is True
        assert calls == ["probed"]
    finally:
        executor.close()


def test_the_security_helper_follows_the_lever_to_the_identity_root(
    monkeypatch, tmp_path
) -> None:
    from tests.security.conftest import route_b_sandbox

    calls = _stub_probe(monkeypatch)
    monkeypatch.setenv("E2B_PURE_ROOTFS", "off")
    monkeypatch.delenv("E2B_REAL_ROOT", raising=False)
    executor, _workspace = route_b_sandbox(None, None)
    try:
        assert executor._has_sandbox_root is False
        assert executor._chroot_root == "/"
        assert executor._real_root is False
        assert calls == []
    finally:
        executor.close()


def test_the_explicit_contradiction_is_refused_by_name() -> None:
    """`E2B_PURE_ROOTFS=synth` + an explicit `E2B_REAL_ROOT=0` cannot work.

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

    from envd_service.app import create_app
    from envd_service.config import (
        PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR,
        check_pure_rootfs_pairing,
    )

    def _settings(pure_rootfs: str, real_root: bool | None) -> SimpleNamespace:
        return SimpleNamespace(pure_rootfs=pure_rootfs, real_root=real_root)

    # The refused shape, and its exact sentence -- written out here so the
    # retreat lever cannot fall out of the message unnoticed.
    assert PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR == (
        "E2B_PURE_ROOTFS=synth without E2B_REAL_ROOT=1: the synthesized root is "
        "an empty skeleton, and only the real root (a mount namespace it binds "
        "into) puts the host system directories, the workspace and the volumes "
        "inside it. With the emulated root every path resolves inside that "
        "skeleton, so the sandbox's own /bin/sh does not exist: the create dies "
        "with errno 13 and every later command answers `instance is closed`. "
        "Drop the explicit E2B_REAL_ROOT=0 so the pair travels together (that is "
        "the default), or set E2B_PURE_ROOTFS=off to keep the pure shape on "
        "N15's identity root."
    )
    with pytest.raises(RuntimeError) as excinfo:
        check_pure_rootfs_pairing(_settings("synth", False))
    assert str(excinfo.value) == PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR

    # The worker refuses to come up with that same sentence, which is the path
    # a deployment actually takes.
    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings=_settings("synth", False))
    assert str(excinfo.value) == PURE_ROOTFS_WITHOUT_REAL_ROOT_ERROR


def test_the_coupled_default_is_not_a_contradiction() -> None:
    """`real_root` unset is the pair, not a half of it: `None` is not `False`."""
    from types import SimpleNamespace

    from envd_service.config import check_pure_rootfs_pairing

    def _settings(pure_rootfs: str, real_root: bool | None) -> SimpleNamespace:
        return SimpleNamespace(pure_rootfs=pure_rootfs, real_root=real_root)

    assert check_pure_rootfs_pairing(_settings("synth", None)) is None
    assert check_pure_rootfs_pairing(_settings("synth", True)) is None
    assert check_pure_rootfs_pairing(_settings("off", False)) is None
    assert check_pure_rootfs_pairing(_settings("off", True)) is None
