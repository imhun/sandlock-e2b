"""Quota-agent worker client protocol + deployment wiring (E2.6)."""

from __future__ import annotations

import logging

import httpx
import pytest

import envd_service.xfs_quota as xfs_quota
from envd_service.app import create_app as create_envd_app
from envd_service.config import Settings as EnvdSettings
from envd_service.quota_agent import QuotaAgentClient, configure_quota_agent_client
from envd_service.runtime.registry import RuntimeRegistry
from envd_service.xfs_quota import ProjectQuotaError

AGENT_URL = "http://quota-agent:49984"
TOKEN = "agent-token"
HEADERS = {"X-Internal-Key": TOKEN}


def _client(handler, *, token: str | None = TOKEN) -> QuotaAgentClient:
    transport = httpx.MockTransport(handler)
    return QuotaAgentClient(AGENT_URL, token=token, transport=transport)


def _facts() -> dict:
    return {
        "fs_type": "xfs",
        "projid32bit": True,
        "prjquota": True,
        "xfs_quota": True,
    }


def test_detect_returns_facts_with_internal_key():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_facts())

    client = _client(handler)
    assert client.detect("/mnt/nfs") == _facts()
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == f"{AGENT_URL}/detect?mount=%2Fmnt%2Fnfs"
    assert requests[0].headers.get("X-Internal-Key") == TOKEN


def test_detect_error_facts_passthrough_for_degradation():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "no mount entry found for /mnt/nfs"})

    client = _client(handler)
    assert client.detect("/mnt/nfs") == {
        "error": "no mount entry found for /mnt/nfs"
    }


def test_detect_non_dict_response_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2, 3])

    client = _client(handler)
    with pytest.raises(ProjectQuotaError) as excinfo:
        client.detect("/mnt/nfs")
    assert str(excinfo.value) == (
        "quota-agent detect returned non-dict response: [1, 2, 3]"
    )


def test_detect_agent_unreachable_raises_quota_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(ProjectQuotaError) as excinfo:
        client.detect("/mnt/nfs")
    assert str(excinfo.value) == "quota-agent unreachable: connection refused"


def test_detect_401_raises_auth_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client(handler)
    with pytest.raises(ProjectQuotaError) as excinfo:
        client.detect("/mnt/nfs")
    assert str(excinfo.value) == "quota-agent auth failed: HTTP 401"


def test_report_returns_projects_table():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "projects": {
                    "7": {"used_blocks": 40, "soft_blocks": 0, "hard_blocks": 1024}
                }
            },
        )

    client = _client(handler)
    assert client.report("/mnt/nfs") == {
        "projects": {"7": {"used_blocks": 40, "soft_blocks": 0, "hard_blocks": 1024}}
    }
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == f"{AGENT_URL}/report?mount=%2Fmnt%2Fnfs"


def test_report_missing_projects_field_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"nope": 1})

    client = _client(handler)
    with pytest.raises(ProjectQuotaError) as excinfo:
        client.report("/mnt/nfs")
    assert str(excinfo.value) == (
        "quota-agent report response missing 'projects': {'nope': 1}"
    )


def test_provision_with_explicit_project_id_posts_project_create():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"projid": 42})

    client = _client(handler)
    projid = client.provision(
        sandbox_id="sbx_nfs",
        project_dir="/mnt/nfs/sbx_nfs",
        disk_mb=2048,
        mount_point="/mnt/nfs",
        project_id=42,
    )
    assert projid == 42
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == f"{AGENT_URL}/project_create"
    assert requests[0].read() == (
        b'{"projid":42,"path":"/mnt/nfs/sbx_nfs",'
        b'"limit_mb":2048,"mount":"/mnt/nfs"}'
    )


def test_provision_allocates_via_report_when_project_id_none():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/report":
            return httpx.Response(200, json={"projects": {}})
        if request.url.path == "/project_create":
            body = request.read().decode()
            projid = int(body.split('"projid":')[1].split(",")[0])
            return httpx.Response(200, json={"projid": projid})
        raise AssertionError(f"unexpected request: {request.url}")

    client = _client(handler)
    projid = client.provision(
        sandbox_id="sbx_nfs",
        project_dir="/mnt/nfs/sbx_nfs",
        disk_mb=1024,
        mount_point="/mnt/nfs",
    )
    assert projid == xfs_quota._hash_projid("sbx_nfs")
    assert len(requests) == 2
    assert [r.url.path for r in requests] == ["/report", "/project_create"]


def test_provision_probes_occupied_projid_against_report():
    requests: list[httpx.Request] = []
    occupied = xfs_quota._hash_projid("sbx_nfs")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/report":
            return httpx.Response(
                200, json={"projects": {str(occupied): {"used_blocks": 1}}}
            )
        if request.url.path == "/project_create":
            body = request.read().decode()
            projid = int(body.split('"projid":')[1].split(",")[0])
            return httpx.Response(200, json={"projid": projid})
        raise AssertionError(f"unexpected request: {request.url}")

    client = _client(handler)
    projid = client.provision(
        sandbox_id="sbx_nfs",
        project_dir="/mnt/nfs/sbx_nfs",
        disk_mb=1024,
        mount_point="/mnt/nfs",
    )
    assert projid == occupied + 1


def test_provision_http_500_wraps_agent_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "limit boom"})

    client = _client(handler)
    with pytest.raises(ProjectQuotaError) as excinfo:
        client.provision(
            sandbox_id="sbx_nfs",
            project_dir="/mnt/nfs/sbx_nfs",
            disk_mb=1024,
            mount_point="/mnt/nfs",
            project_id=42,
        )
    assert str(excinfo.value) == "quota-agent project_create failed: limit boom"


def test_provision_response_missing_projid_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    with pytest.raises(ProjectQuotaError) as excinfo:
        client.provision(
            sandbox_id="sbx_nfs",
            project_dir="/mnt/nfs/sbx_nfs",
            disk_mb=1024,
            mount_point="/mnt/nfs",
            project_id=42,
        )
    assert str(excinfo.value) == (
        "quota-agent project_create response missing int 'projid': {'ok': True}"
    )


def test_release_posts_project_delete():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"deleted": 42})

    client = _client(handler)
    client.release(
        project_dir="/mnt/nfs/sbx_nfs",
        mount_point="/mnt/nfs",
        projid=42,
    )
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == f"{AGENT_URL}/project_delete"
    assert requests[0].read() == (
        b'{"projid":42,"path":"/mnt/nfs/sbx_nfs","mount":"/mnt/nfs"}'
    )


def test_reconcile_posts_workspace_and_mount():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200, json={"cleaned": [7], "skipped": [{"projid": 9, "reason": "busy"}]}
        )

    client = _client(handler)
    result = client.reconcile(
        workspace_base="/mnt/nfs",
        mount_point="/mnt/nfs",
    )
    assert result == {"cleaned": [7], "skipped": [{"projid": 9, "reason": "busy"}]}
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == f"{AGENT_URL}/reconcile"
    assert requests[0].read() == (
        b'{"workspace_base":"/mnt/nfs","mount":"/mnt/nfs"}'
    )


def test_reconcile_missing_fields_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"cleaned": [1]})

    client = _client(handler)
    with pytest.raises(ProjectQuotaError) as excinfo:
        client.reconcile(workspace_base="/mnt/nfs", mount_point="/mnt/nfs")
    assert str(excinfo.value) == (
        "quota-agent reconcile response missing 'cleaned'/'skipped' lists: "
        "{'cleaned': [1]}"
    )


def test_configure_wires_agent_query_and_ops(monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_query", None)
    monkeypatch.setattr(xfs_quota, "agent_ops", None)
    client = configure_quota_agent_client(
        url=AGENT_URL,
        token=TOKEN,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_facts())
        ),
    )
    try:
        assert client is not None
        assert xfs_quota.agent_query == client.detect
        assert set(xfs_quota.agent_ops) == {
            "provision",
            "release",
            "report",
            "reconcile",
        }
        assert xfs_quota.agent_ops["provision"] == client.provision
        assert xfs_quota.xfs_project_supported("/mnt/nfs", via_agent=True) == (
            True,
            "",
        )
    finally:
        configure_quota_agent_client(url=None)
        if client is not None:
            client.close()


def test_configure_without_url_resets_hooks_and_warns(caplog, monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_query", lambda _mp: {})
    monkeypatch.setattr(xfs_quota, "agent_ops", {"provision": lambda **_kw: 1})
    caplog.set_level(logging.WARNING)
    client = configure_quota_agent_client(url=None)
    assert client is None
    assert xfs_quota.agent_query is None
    assert xfs_quota.agent_ops is None
    assert [r.message for r in caplog.records] == [
        "E2B_QUOTA_VIA_AGENT is enabled but E2B_QUOTA_AGENT_URL is not set; "
        "quota-agent hooks unconfigured, quota operations will degrade with "
        "warnings",
    ]


def test_configure_without_token_warns(caplog):
    caplog.set_level(logging.WARNING)
    client = configure_quota_agent_client(
        url=AGENT_URL,
        token=None,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_facts())
        ),
    )
    try:
        assert client is not None
        assert [r.message for r in caplog.records] == [
            "E2B_QUOTA_AGENT_TOKEN is not set; quota-agent requests carry no "
            "X-Internal-Key and will be rejected (401) until configured",
        ]
    finally:
        configure_quota_agent_client(url=None)
        if client is not None:
            client.close()


def test_worker_provision_project_via_agent_end_to_end(monkeypatch):
    """via_agent=True + configured hooks -> HTTP, local path untouched."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/report":
            return httpx.Response(200, json={"projects": {}})
        if request.url.path == "/project_create":
            return httpx.Response(200, json={"projid": 4242})
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(xfs_quota, "agent_query", None)
    monkeypatch.setattr(xfs_quota, "agent_ops", None)
    client = configure_quota_agent_client(
        url=AGENT_URL,
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    try:
        projid = xfs_quota.provision_project(
            sandbox_id="sbx_nfs",
            project_dir="/mnt/nfs/sbx_nfs",
            mount_point="/mnt/nfs",
            disk_mb=1024,
            via_agent=True,
        )
        assert projid == 4242
        assert [r.url.path for r in requests] == ["/report", "/project_create"]
    finally:
        configure_quota_agent_client(url=None)
        if client is not None:
            client.close()


def test_worker_release_project_via_agent_end_to_end(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"deleted": 7})

    monkeypatch.setattr(xfs_quota, "agent_query", None)
    monkeypatch.setattr(xfs_quota, "agent_ops", None)
    client = configure_quota_agent_client(
        url=AGENT_URL,
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    try:
        xfs_quota.release_project(
            project_dir="/mnt/nfs/sbx_nfs",
            mount_point="/mnt/nfs",
            projid=7,
            via_agent=True,
        )
        assert [r.url.path for r in requests] == ["/project_delete"]
    finally:
        configure_quota_agent_client(url=None)
        if client is not None:
            client.close()


async def test_create_app_wires_agent_hooks_when_via_agent(workspace, monkeypatch):
    monkeypatch.setattr(xfs_quota, "agent_query", None)
    monkeypatch.setattr(xfs_quota, "agent_ops", None)
    app = create_envd_app(
        settings=EnvdSettings(
            executor="local",
            workspace_base=workspace,
            quota_via_agent=True,
            quota_agent_url=AGENT_URL,
            quota_agent_token=TOKEN,
        ),
        runtime_registry=RuntimeRegistry(workspace),
    )
    try:
        client = app.state.quota_agent_client
        assert client is not None
        assert xfs_quota.agent_query == client.detect
        assert set(xfs_quota.agent_ops) == {
            "provision",
            "release",
            "report",
            "reconcile",
        }
    finally:
        configure_quota_agent_client(url=None)
        client = app.state.quota_agent_client
        if client is not None:
            client.close()


async def test_create_app_via_agent_without_url_degrades(
    workspace, monkeypatch, caplog
):
    monkeypatch.setattr(xfs_quota, "agent_query", lambda _mp: {})
    monkeypatch.setattr(xfs_quota, "agent_ops", {"provision": lambda **_kw: 1})
    caplog.set_level(logging.WARNING)
    app = create_envd_app(
        settings=EnvdSettings(
            executor="local",
            workspace_base=workspace,
            quota_via_agent=True,
        ),
        runtime_registry=RuntimeRegistry(workspace),
    )
    assert app.state.quota_agent_client is None
    assert xfs_quota.agent_query is None
    assert xfs_quota.agent_ops is None
    assert [r.message for r in caplog.records] == [
        "E2B_QUOTA_VIA_AGENT is enabled but E2B_QUOTA_AGENT_URL is not set; "
        "quota-agent hooks unconfigured, quota operations will degrade with "
        "warnings",
    ]
