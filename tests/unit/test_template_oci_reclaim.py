"""Task 2: the shared `_oci/` tar reclaim belongs to the control plane."""

from __future__ import annotations

import pytest

from envd_service.runtime.image_resolver import local_oci_paths

IMAGE = "byteplan/template-demo:latest"


def _make_tar(platform_root):
    tar, link = local_oci_paths(platform_root / "_images", IMAGE)
    tar.parent.mkdir(parents=True, exist_ok=True)
    tar.write_bytes(b"oci")
    link.write_text("ink", encoding="utf-8")
    return tar, link


def test_reclaim_removes_the_tar_and_its_sidecar(tmp_path):
    from control_plane.api.templates import reclaim_oci_artifacts

    tar, link = _make_tar(tmp_path)
    removed = reclaim_oci_artifacts(tmp_path, IMAGE, known_images={IMAGE})
    assert removed == [tar, link]
    assert tar.exists() is False
    assert link.exists() is False


def test_reclaim_refuses_an_image_no_template_names(tmp_path):
    from control_plane.api.templates import reclaim_oci_artifacts

    tar, link = _make_tar(tmp_path)
    with pytest.raises(ValueError) as err:
        reclaim_oci_artifacts(tmp_path, IMAGE, known_images=set())
    assert str(err.value) == (
        f"refusing to reclaim the OCI artifacts of unnamed image {IMAGE}"
    )
    assert tar.exists() is True
    assert link.exists() is True


def test_the_discard_path_is_the_wired_caller(tmp_path, monkeypatch):
    """Pin the wiring, not the pure function.

    ``TemplateRegistry.discard`` is the only path in the process that forgets a
    template record -- there is no template delete/rename endpoint to hang the
    reclaim off -- and its one caller is ``_discard_failed_build``. That caller
    must hand over the export root (``app.state.platform_root``) and the live
    record set (``templates.list()``, never an mtime).
    """
    from types import SimpleNamespace

    from control_plane.api import templates as api_templates
    from control_plane.registry.templates import TemplateRegistry

    registry = TemplateRegistry(tmp_path / "_templates")
    live, _ = registry.create("live")
    doomed, _ = registry.create("doomed")
    root = tmp_path
    doomed_tar, doomed_link = local_oci_paths(root / "_images", doomed.image)
    doomed_tar.parent.mkdir(parents=True, exist_ok=True)
    doomed_tar.write_bytes(b"oci")
    doomed_link.write_text("ink", encoding="utf-8")
    live_tar, _ = local_oci_paths(root / "_images", live.image)
    live_tar.write_bytes(b"oci")

    calls = []
    real = api_templates.reclaim_oci_artifacts

    def spy(platform_root, image, *, known_images):
        calls.append((platform_root, image, set(known_images)))
        return real(platform_root, image, known_images=known_images)

    monkeypatch.setattr(api_templates, "reclaim_oci_artifacts", spy)
    app = SimpleNamespace(
        state=SimpleNamespace(platform_root=root, templates=registry)
    )

    api_templates._discard_failed_build(app, doomed)

    # The judged set is taken while the doomed record is still live: it is the
    # record that names the image the artifacts belong to.
    assert calls == [(root, doomed.image, {live.image, doomed.image})]
    assert doomed.discarded is True
    assert doomed_tar.exists() is False
    assert doomed_link.exists() is False
    assert live_tar.exists() is True
