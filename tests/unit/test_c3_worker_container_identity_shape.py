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
    """Off the service account (compose), the anchor is still reported/warned."""
    _compose(monkeypatch)
    assert wi.container_identity_anchor_expected() is True
    # A compose worker's hostname is not a container id here (the very shape the
    # warning exists for): the raw probe runs and names the operator's mistake.
    monkeypatch.setattr(wi.socket, "gethostname", lambda: "e2b-worker-1")
    with caplog.at_level(logging.WARNING, logger="envd_service.worker_identity"):
        assert wi.reported_container_id() is None
    assert "is not a container id" in caplog.text


def test_the_compose_shape_reports_the_kernel_hostname(monkeypatch) -> None:
    _compose(monkeypatch)
    monkeypatch.setattr(
        wi.socket, "gethostname", lambda: "e4a98a0c5282"
    )
    assert wi.reported_container_id() == "e4a98a0c5282"
