"""F2: only the shape that *needs* the container-id anchor reports it.

Ruling D25's anchor is the worker's hostname-as-container-id, and only the
**compose** lane uses it -- there the control plane has no pod spec to read, so
the agent confirms the worker's identity against the worker's host-side cgroup
path. The **k8s** lane verifies the identity from the pod spec's
``runAsUser``/``runAsGroup`` instead, and a k8s pod's hostname is always its
*pod name* (``e2b-worker-1``), which can never be a container id. Probing for
one there produced a warning that was simply false -- "the control plane will
refuse every C3 file operation on this node by name" -- on a node where every
file operation succeeded.

These tests pin the gate: the k8s shape neither probes nor warns, and the
compose shape keeps probing and keeps its warning.
"""

from __future__ import annotations

import logging

import envd_service.worker_identity as wi


def _boom() -> str:
    raise AssertionError(
        "the k8s shape must not probe for a container id it cannot have"
    )


def _k8s(monkeypatch) -> None:
    monkeypatch.setenv("E2B_NODE_ADDRESS_MODE", "k8s")


def _compose(monkeypatch) -> None:
    monkeypatch.setenv("E2B_NODE_ADDRESS_MODE", "hostname")


def test_the_k8s_shape_does_not_probe_for_a_container_id(monkeypatch) -> None:
    _k8s(monkeypatch)
    assert wi.container_identity_anchor_expected() is False
    monkeypatch.setattr(wi, "worker_container_id", _boom)
    assert wi.reported_container_id() is None


def test_the_k8s_shape_does_not_emit_the_container_id_warning(
    monkeypatch, caplog
) -> None:
    _k8s(monkeypatch)
    monkeypatch.setattr(wi, "worker_container_id", _boom)
    with caplog.at_level(logging.WARNING, logger="envd_service.worker_identity"):
        assert wi.reported_container_id() is None
    assert "container identity to report" not in caplog.text
    assert caplog.records == []


def test_auto_under_a_service_account_is_the_k8s_shape(
    monkeypatch, caplog, tmp_path
) -> None:
    """The worker's env usually omits the mode; in-cluster detection decides."""
    monkeypatch.delenv("E2B_NODE_ADDRESS_MODE", raising=False)
    token = tmp_path / "token"
    token.write_text("x", encoding="utf-8")
    monkeypatch.setattr(wi, "_SERVICE_ACCOUNT_TOKEN", token)
    monkeypatch.setattr(wi, "worker_container_id", _boom)
    with caplog.at_level(logging.WARNING, logger="envd_service.worker_identity"):
        assert wi.reported_container_id() is None
    assert caplog.records == []


def test_the_compose_shape_keeps_probing_and_its_warning(
    monkeypatch, caplog
) -> None:
    """Off the service account (compose), the anchor is still reported/warned.

    Both routes have to come up empty for the warning -- the hostname here and
    the mount table below it, which is stubbed out because a unit test running
    *inside* a container would otherwise find the engine's own container id
    there and legitimately report it (the ``…_from_the_mount_table`` case).
    """
    _compose(monkeypatch)
    assert wi.container_identity_anchor_expected() is True
    # A compose worker's hostname is not a container id here (the very shape the
    # warning exists for): the raw probe runs and names the operator's mistake.
    monkeypatch.setattr(wi.socket, "gethostname", lambda: "e2b-worker-1")
    monkeypatch.setattr(wi, "_container_id_from_mountinfo", lambda *a, **kw: None)
    with caplog.at_level(logging.WARNING, logger="envd_service.worker_identity"):
        assert wi.reported_container_id() is None
    assert "is not a container id" in caplog.text


def test_the_container_id_comes_from_the_mount_table_when_the_hostname_cannot(
    monkeypatch, caplog
) -> None:
    """An engine that ignores Docker's hostname default is not operator error.

    OrbStack gives every container ``hostname=orbstack`` (measured 2026-10-10),
    which is not a container id -- so the hostname route reports nothing on a
    host where the kernel *does* name the id, in the root mount's ``upperdir``.
    The fallback returns it and says so at INFO, not as a WARNING naming a
    mistake the operator did not make.
    """
    _compose(monkeypatch)
    monkeypatch.setattr(wi.socket, "gethostname", lambda: "orbstack")
    monkeypatch.setattr(
        wi,
        "_container_id_from_mountinfo",
        lambda *a, **kw: "b3fed2b30c393ffdae3e1e801f7b60f4ec5cbb1f2bcb3cc11329a6fbce69139c",
    )
    with caplog.at_level(logging.INFO, logger="envd_service.worker_identity"):
        assert (
            wi.reported_container_id()
            == "b3fed2b30c393ffdae3e1e801f7b60f4ec5cbb1f2bcb3cc11329a6fbce69139c"
        )
    # No WARNING: the operator did not make the mistake that one names (the INFO
    # above does quote the hostname, so the level is what distinguishes them).
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert "has no container identity to report" not in caplog.text


def test_the_mount_table_route_reads_the_engines_upperdir(tmp_path) -> None:
    """The parse itself, against a real Docker ``mountinfo`` line."""
    line = (
        "3630 1045 0:49 / / rw,relatime - overlay overlay rw,"
        "lowerdir=/var/lib/docker/overlay2/l/6RINDRNSBZ7P6YYCVVCDLB7F4C:"
        "/var/lib/docker/overlay2/l/DEOZW3QXK3VFPO3ZE5NPTZU4QN,"
        "upperdir=/var/lib/docker/overlay2/"
        "b3fed2b30c393ffdae3e1e801f7b60f4ec5cbb1f2bcb3cc11329a6fbce69139c/diff,"
        "workdir=/var/lib/docker/overlay2/"
        "b3fed2b30c393ffdae3e1e801f7b60f4ec5cbb1f2bcb3cc11329a6fbce69139c/work\n"
    )
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "mountinfo").write_text(line, encoding="utf-8")
    assert wi._container_id_from_mountinfo(proc) == (
        "b3fed2b30c393ffdae3e1e801f7b60f4ec5cbb1f2bcb3cc11329a6fbce69139c"
    )


def test_the_compose_shape_reports_the_kernel_hostname(monkeypatch) -> None:
    _compose(monkeypatch)
    monkeypatch.setattr(
        wi.socket, "gethostname", lambda: "e4a98a0c5282"
    )
    assert wi.reported_container_id() == "e4a98a0c5282"
