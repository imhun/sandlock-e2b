"""N73: ``E2B_SHARED_VOLUME_ROOT`` is the shared *export* root, not the store.

One name carried two meanings: on the worker it is the export root the agent
scopes hostPaths to (``_snapshots`` / ``_migrate`` / ``_volumes`` sit *below*
it), while ``control_plane/app.py`` read it as the volume **store root itself**
(``volume_root = settings.shared_volume_root or platform_root / "_volumes"``).
A deployment that set it on the control plane therefore built volumes at
``<export>/vol_xxx`` -- and on k8s ``<export>`` is the read-only mount, so
``0.1.0-943`` answered the first ``POST /volumes`` with ``500``/``EROFS``.

Task 20 splits the two meanings:

* ``E2B_VOLUME_STORE_ROOT`` (new) is the authoritative store root.
* ``E2B_SHARED_VOLUME_ROOT`` on the control plane only judges/derives: unset
  store root + named shared root ⇒ ``<shared_volume_root>/_volumes``.
* neither named ⇒ ``<platform_root>/"_volumes"`` (today's k8s shape, unchanged).

The self-protection is the reason this is not a silent path move: the compose
stacks set ``E2B_SHARED_VOLUME_ROOT`` **today**, so a deployment with old flat
``<shared_volume_root>/vol_*`` directories must refuse to start by name rather
than have those volumes vanish from the API. The escape hatch -- naming the old
flat directory outright with ``E2B_VOLUME_STORE_ROOT`` -- stays legal, because
then the volumes are exactly where the store looks and nothing is hidden.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from control_plane.app import LegacyVolumeLayoutError
from control_plane.app import create_app as create_control_app
from control_plane.config import Settings as ControlSettings

API_KEY = "local-key"


def _settings(tmp_path: Path, **overrides) -> ControlSettings:
    workspace = tmp_path / "export"
    workspace.mkdir(exist_ok=True)
    kwargs = dict(
        api_keys=(API_KEY,),
        workspace_base=workspace,
        eviction_enabled=False,
        create_queue_timeout_s=0,
    )
    kwargs.update(overrides)
    return ControlSettings(**kwargs)


def _store_root(tmp_path: Path, **overrides) -> Path:
    app = create_control_app(
        settings=_settings(tmp_path, **overrides),
        workspace_base=(tmp_path / "export"),
    )
    return app.state.volumes._base


def test_shared_volume_root_alone_derives_a_volumes_subdirectory(tmp_path) -> None:
    """(a) N73's core: the shared root is the export root, so the store is
    ``<shared_volume_root>/_volumes`` -- not the shared root itself (today's
    ``volume_root = shared_volume_root`` would put it on the read-only mount)."""
    shared = tmp_path / "shared"
    shared.mkdir()
    assert _store_root(tmp_path, shared_volume_root=str(shared)) == (
        shared / "_volumes"
    ).resolve()


def test_a_named_store_root_wins_outright(tmp_path) -> None:
    """(b) The dedicated variable is the authority, whatever the shared root is."""
    shared = tmp_path / "shared"
    shared.mkdir()
    store = tmp_path / "store"
    store.mkdir()
    assert _store_root(
        tmp_path,
        shared_volume_root=str(shared),
        volume_store_root=str(store),
    ) == store.resolve()


def test_neither_variable_keeps_the_platform_root_derivation(tmp_path) -> None:
    """(c) The k8s shape: neither name set ⇒ ``<platform_root>/"_volumes"``."""
    shared_workspace = tmp_path / "shared-workspace"
    shared_workspace.mkdir()
    app = create_control_app(
        settings=_settings(tmp_path, shared_workspace_root=str(shared_workspace)),
        workspace_base=(tmp_path / "export"),
    )
    assert app.state.platform_root == shared_workspace.resolve()
    assert app.state.volumes._base == shared_workspace.resolve() / "_volumes"


def test_legacy_flat_volumes_refuse_startup_by_name(tmp_path) -> None:
    """(d) An old ``<shared_volume_root>/vol_*`` directory is the pre-N73 layout.

    Deriving ``<shared>/_volumes`` would leave it invisible, so the control
    plane refuses to start and names the migration instead.
    """
    shared = tmp_path / "shared"
    legacy = shared / "vol_old"
    legacy.mkdir(parents=True)
    with pytest.raises(LegacyVolumeLayoutError) as caught:
        _store_root(tmp_path, shared_volume_root=str(shared))
    message = str(caught.value)
    assert str(legacy) in message
    assert "N73" in message


def test_naming_the_legacy_flat_root_itself_is_not_hidden(tmp_path) -> None:
    """The escape hatch: ``E2B_VOLUME_STORE_ROOT=<shared_volume_root>`` keeps the
    flat volumes exactly where the store looks, so nothing is hidden and the
    self-protection has nothing to refuse."""
    shared = tmp_path / "shared"
    (shared / "vol_old").mkdir(parents=True)
    assert _store_root(
        tmp_path,
        shared_volume_root=str(shared),
        volume_store_root=str(shared),
    ) == shared.resolve()
