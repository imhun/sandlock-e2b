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
from envd_service.config import (
    RETIRED_PURE_ROOTFS_LEVER_ERROR,
    RETIRED_REAL_ROOT_LEVER_ERROR,
    Settings,
    refuse_retired_root_levers,
)


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


# -- N14 S5: the two retro levers are retired ------------------------------
#
# `E2B_PURE_ROOTFS=off` (N15's identity root) and `E2B_REAL_ROOT=0` (the
# emulated root) used to be the two ways back to the shape S5 deletes. They are
# refused **by name at startup** instead of quietly served: a deployment that
# still writes them must be told which line to delete, because the rest of the
# fleet no longer tests that shape.


def test_the_retreat_lever_is_retired_and_refused_by_name(monkeypatch) -> None:
    monkeypatch.setenv("E2B_PURE_ROOTFS", "off")

    with pytest.raises(RuntimeError) as excinfo:
        refuse_retired_root_levers(Settings())

    assert str(excinfo.value) == RETIRED_PURE_ROOTFS_LEVER_ERROR.format(value="off")
    assert "delete the line" in str(excinfo.value)


def test_the_emulated_root_is_retired_and_refused_by_name(monkeypatch) -> None:
    monkeypatch.setenv("E2B_REAL_ROOT", "0")

    with pytest.raises(RuntimeError) as excinfo:
        refuse_retired_root_levers(Settings())

    assert str(excinfo.value) == RETIRED_REAL_ROOT_LEVER_ERROR
    assert "delete the line" in str(excinfo.value)


def test_unset_and_on_are_the_only_accepted_spellings(monkeypatch) -> None:
    monkeypatch.delenv("E2B_REAL_ROOT", raising=False)
    monkeypatch.delenv("E2B_PURE_ROOTFS", raising=False)
    assert refuse_retired_root_levers(Settings()) is None

    monkeypatch.setenv("E2B_REAL_ROOT", "1")
    monkeypatch.setenv("E2B_PURE_ROOTFS", "synth")
    assert refuse_retired_root_levers(Settings()) is None


def test_the_real_root_reading_is_kept_but_nothing_branches_on_it(monkeypatch) -> None:
    """`Settings.real_root` still reports the raw env; the *shape* is not a knob."""
    monkeypatch.setenv("E2B_REAL_ROOT", "1")
    assert Settings().real_root is True


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


def test_the_factory_has_no_shape_without_a_real_root(monkeypatch, tmp_path) -> None:
    """N14 S5: the emulated root is gone, so no shape builds without the real one.

    `E2B_PURE_ROOTFS=off` is refused by the app at startup (pinned below); this
    is the second half -- a hand-built executor cannot select the emulated root
    either, because the branch that produced it no longer exists.
    """
    _stub_probe(monkeypatch)
    settings, executor = _factory(monkeypatch, tmp_path, switch="off")

    assert settings.pure_rootfs == "off"
    assert executor._real_root is True


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


def test_the_image_shape_gets_the_real_root_too(
    monkeypatch, tmp_path
) -> None:
    """An image sandbox has no synthesized root, but it does have the real one.

    `E2B_BASE_IMAGE` is set by both production manifests, so this is the fleet's
    shape: `E2B_REAL_ROOT` used to decide whether it got the emulated root; that
    value is retired (N14 S5), so the root is simply part of the shape.
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
    assert executor._real_root is True
    # And it now asks the worker whether it can do it: the real root used to be
    # armed only for the pure shape, so this shape never probed the capability.
    assert calls == ["probed"]


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


def test_the_app_refuses_a_retired_lever_at_startup() -> None:
    """The path a deployment takes: `create_app` with one of the old values.

    Both spellings are refused, and the refusal names the line to delete -- the
    values are the defaults now, so deleting it is the whole migration.
    """
    from types import SimpleNamespace

    from envd_service.app import create_app

    def _settings(pure_rootfs: str, real_root: bool | None) -> SimpleNamespace:
        return SimpleNamespace(pure_rootfs=pure_rootfs, real_root=real_root)

    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings=_settings("synth", False))
    assert str(excinfo.value) == RETIRED_REAL_ROOT_LEVER_ERROR

    with pytest.raises(RuntimeError) as excinfo:
        create_app(settings=_settings("off", None))
    assert str(excinfo.value) == RETIRED_PURE_ROOTFS_LEVER_ERROR.format(value="off")

    # The defaults are not a refusal: unset/on (and `None` for the raw
    # real-root reading) come up.
    assert refuse_retired_root_levers(_settings("synth", None)) is None
    assert refuse_retired_root_levers(_settings("synth", True)) is None
