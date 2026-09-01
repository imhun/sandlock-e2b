"""E3.1: tenant/key mapping resolution and authorization helpers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from control_plane.api.errors import OfficialError
from control_plane.auth import _require_owned, _require_related, tenant_of
from control_plane.config import Settings


def _settings(**overrides) -> Settings:
    defaults = dict(api_keys=("local-key",))
    defaults.update(overrides)
    return Settings(**defaults)


def _request(settings: Settings, key: str):
    headers = {"X-API-Key": key}
    return SimpleNamespace(headers=headers, app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


def test_compat_mode_all_keys_are_non_admin_no_tenant():
    settings = _settings()
    assert settings.tenants_enabled is False
    tenant, is_admin = tenant_of(_request(settings, "local-key"))
    assert (tenant, is_admin) == (None, False)


def test_tenant_key_resolves_tenant():
    settings = _settings(tenant_map={"t1": ["keyA", "keyB"], "t2": ["keyC"]})
    assert settings.tenants_enabled is True
    assert tenant_of(_request(settings, "keyA")) == ("t1", False)
    assert tenant_of(_request(settings, "keyB")) == ("t1", False)
    assert tenant_of(_request(settings, "keyC")) == ("t2", False)


def test_admin_key_is_admin_and_bypasses_tenant():
    settings = _settings(
        tenant_map={"t1": ["keyA"]}, admin_api_keys=("admin-key",)
    )
    assert tenant_of(_request(settings, "admin-key")) == (None, True)


def test_admin_wins_over_tenant_mapping():
    settings = _settings(
        tenant_map={"t1": ["shared-key"]}, admin_api_keys=("shared-key",)
    )
    assert tenant_of(_request(settings, "shared-key")) == (None, True)


def test_unmapped_key_has_no_tenant():
    settings = _settings(
        tenant_map={"t1": ["keyA"]}, admin_api_keys=("admin-key",)
    )
    assert tenant_of(_request(settings, "other-key")) == (None, False)


def test_all_api_keys_includes_tenant_and_admin_keys():
    settings = _settings(
        api_keys=("legacy-key",),
        api_key="single-key",
        tenant_map={"t1": ["keyA"], "t2": ["keyC"]},
        admin_api_keys=("admin-key",),
    )
    assert set(settings.all_api_keys) == {
        "legacy-key",
        "single-key",
        "keyA",
        "keyC",
        "admin-key",
    }


def test_tenant_map_requires_list_values():
    with pytest.raises(ValueError, match="must be a list"):
        Settings(api_keys=("k",), tenant_map={"t1": "keyA"})


class _FakeRecord:
    def __init__(self, tenant_id=None):
        self.tenant_id = tenant_id


def test_require_owned_passes_for_owner_and_admin_and_compat():
    settings = _settings(tenant_map={"t1": ["keyA"]}, admin_api_keys=("admin-key",))
    record = _FakeRecord(tenant_id="t1")
    _require_owned(_request(settings, "keyA"), record, resource_id="r1", label="Sandbox")
    _require_owned(_request(settings, "admin-key"), record)
    compat = _settings()
    _require_owned(_request(compat, "local-key"), record)


def test_require_owned_404_on_mismatch():
    settings = _settings(tenant_map={"t1": ["keyA"], "t2": ["keyC"]})
    record = _FakeRecord(tenant_id="t2")
    with pytest.raises(OfficialError) as exc:
        _require_owned(
            _request(settings, "keyA"), record, resource_id="sbx_x", label="Sandbox"
        )
    assert exc.value.code == 404
    assert exc.value.message == "Sandbox sbx_x not found"


def test_require_owned_404_matches_missing_resource_message():
    settings = _settings(tenant_map={"t1": ["keyA"], "t2": ["keyC"]})
    record = _FakeRecord(tenant_id="t2")
    missing = OfficialError(404, "Sandbox sbx_x not found")
    with pytest.raises(OfficialError) as exc:
        _require_owned(
            _request(settings, "keyA"), record, resource_id="sbx_x", label="Sandbox"
        )
    assert (exc.value.code, exc.value.message) == (missing.code, missing.message)


def test_require_related_403_on_cross_tenant():
    settings = _settings(tenant_map={"t1": ["keyA"], "t2": ["keyC"]})
    record = _FakeRecord(tenant_id="t2")
    with pytest.raises(OfficialError) as exc:
        _require_related(
            _request(settings, "keyA"), record, resource_id="vol_x", label="Volume"
        )
    assert exc.value.code == 403
    assert exc.value.message == "Volume vol_x does not belong to this tenant"


def test_require_related_allows_admin():
    settings = _settings(tenant_map={"t1": ["keyA"]}, admin_api_keys=("admin-key",))
    record = _FakeRecord(tenant_id="t1")
    _require_related(_request(settings, "admin-key"), record)
