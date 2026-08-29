"""Sandbox lifecycle via the official e2b SDK."""

from __future__ import annotations

import pytest

from e2b import Sandbox


def test_lifecycle_sync(live_servers):
    sandbox = Sandbox.create()
    try:
        assert sandbox.sandbox_id.startswith("sbx_")
        assert sandbox.is_running() is True
        info = sandbox.get_info()
        assert info.sandbox_id == sandbox.sandbox_id
        assert info.state == "running"
        assert info.envd_version == "0.6.4+sandlock"
    finally:
        assert sandbox.kill() is True


@pytest.mark.asyncio
async def test_lifecycle_async(async_sandbox):
    sandbox = async_sandbox
    assert sandbox.sandbox_id.startswith("sbx_")
    assert (await sandbox.is_running()) is True
    info = await sandbox.get_info()
    assert info.sandbox_id == sandbox.sandbox_id
    assert info.state == "running"


def test_kill_then_not_running(live_servers):
    sandbox = Sandbox.create()
    assert sandbox.is_running() is True
    assert sandbox.kill() is True
    assert sandbox.is_running() is False


def test_get_info_missing_raises(live_servers):
    from e2b.exceptions import SandboxNotFoundException

    with pytest.raises(SandboxNotFoundException):
        Sandbox.get_info("sbx_missing")


def test_kill_missing_returns_false(live_servers):
    assert Sandbox.kill("sbx_missing") is False


def test_connect_returns_same_sandbox(live_servers):
    created = Sandbox.create()
    try:
        connected = Sandbox.connect(created.sandbox_id, timeout=600)
        assert connected.sandbox_id == created.sandbox_id
        result = connected.commands.run("echo reconnected")
        assert result.stdout == "reconnected\n"
        assert result.exit_code == 0
    finally:
        created.kill()


def test_set_timeout(live_servers):
    sandbox = Sandbox.create()
    try:
        before = sandbox.get_info().end_at
        sandbox.set_timeout(600)
        after = sandbox.get_info().end_at
        assert after > before
    finally:
        sandbox.kill()


def test_list_pagination_includes_sandbox(live_servers):
    created = Sandbox.create()
    try:
        paginator = Sandbox.list(limit=10)
        ids = []
        while paginator.has_next:
            ids.extend(s.sandbox_id for s in paginator.next_items())
        assert created.sandbox_id in ids
    finally:
        created.kill()


def test_list_metadata_filter(live_servers):
    sandbox = Sandbox.create(metadata={"pytest": "true"})
    try:
        from e2b.sandbox.sandbox_api import SandboxQuery

        paginator = Sandbox.list(query=SandboxQuery(metadata={"pytest": "true"}), limit=10)
        ids = []
        while paginator.has_next:
            ids.extend(s.sandbox_id for s in paginator.next_items())
        assert sandbox.sandbox_id in ids
    finally:
        sandbox.kill()


def test_create_with_template_image(live_servers):
    sandbox = Sandbox.create(template="py311")
    try:
        assert sandbox.sandbox_id.startswith("sbx_")
        result = sandbox.commands.run("echo template-ok")
        assert result.stdout == "template-ok\n"
        assert result.exit_code == 0
    finally:
        sandbox.kill()


def test_env_vars_injected(live_servers):
    sandbox = Sandbox.create(envs={"MY_TEST_VAR": "42"})
    try:
        result = sandbox.commands.run("echo $MY_TEST_VAR")
        assert result.stdout == "42\n"
        assert result.exit_code == 0
    finally:
        sandbox.kill()


def test_metadata_filter_via_query(live_servers):
    sandbox = Sandbox.create(metadata={"team": "sdk"})
    try:
        from e2b.sandbox.sandbox_api import SandboxQuery

        paginator = Sandbox.list(query=SandboxQuery(metadata={"team": "sdk"}))
        while paginator.has_next:
            items = paginator.next_items()
            assert any(s.sandbox_id == sandbox.sandbox_id for s in items)
    finally:
        sandbox.kill()

