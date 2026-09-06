"""Volume, Secret, Pause/Resume and Metrics via the official SDK."""

from __future__ import annotations

import time

import pytest

from e2b import Sandbox, Secret, Volume


def test_volume_roundtrip_and_mount(live_servers):
    volume = Volume.create("sdk-volume")
    try:
        assert volume.volume_id.startswith("vol_")
        assert volume.name == "sdk-volume"
        assert volume.token

        volume.write_file("data.txt", b"persist-me")
        assert volume.read_file("data.txt") == "persist-me"

        sandbox = Sandbox.create(volume_mounts={"mnt/data": volume.volume_id})
        try:
            result = sandbox.commands.run("cat mnt/data/data.txt")
            assert result.stdout == "persist-me"
            assert result.exit_code == 0
        finally:
            sandbox.kill()

        # Persists across sandbox lifetimes.
        second = Sandbox.create(volume_mounts={"mnt/data": volume.volume_id})
        try:
            result = second.commands.run("cat mnt/data/data.txt")
            assert result.stdout == "persist-me"
        finally:
            second.kill()
    finally:
        Volume.destroy(volume.volume_id)


def test_volume_get_info_and_list(live_servers):
    volume = Volume.create("sdk-volume-list")
    try:
        info = Volume.get_info(volume.volume_id)
        assert info.volume_id == volume.volume_id
        assert info.name == "sdk-volume-list"
        assert info.token
        assert any(v.volume_id == volume.volume_id for v in Volume.list())
    finally:
        Volume.destroy(volume.volume_id)


def test_volume_mount_paths_unified_absolute_and_relative(live_servers):
    """Mount path rules are identical across executors/templates: the path is
    normalized relative to the sandbox root and commands read it with a
    relative path from the default cwd."""
    volume = Volume.create("path-vol")
    try:
        volume.write_file("data.txt", b"path-data")
        for template, mount_path in (
            ("base", "/mnt/data"),
            ("base", "mnt/data"),
            ("py311", "/mnt/data"),
            ("py311", "mnt/data"),
        ):
            sandbox = Sandbox.create(
                template=template, volume_mounts={mount_path: volume.volume_id}
            )
            try:
                result = sandbox.commands.run("cat mnt/data/data.txt")
                assert result.stdout == "path-data"
                assert result.exit_code == 0
            finally:
                sandbox.kill()
    finally:
        Volume.destroy(volume.volume_id)


def test_secret_injected_into_env(live_servers):
    secret = Secret.create("sdksecret", "secret-answer", metadata={"source": "test"})
    try:
        sandbox = Sandbox.create(envs={"FROM_SECRET": "${sdksecret}"})
        try:
            result = sandbox.commands.run("echo $FROM_SECRET")
            assert result.stdout == "secret-answer\n"
            assert result.exit_code == 0
        finally:
            sandbox.kill()
    finally:
        Secret.destroy(secret.secret_id)


def test_pause_and_resume_via_connect(live_servers):
    sandbox = Sandbox.create()
    try:
        assert sandbox.pause() is True
        assert sandbox.pause() is False  # already paused -> 409
        info = Sandbox.get_info(sandbox.sandbox_id)
        assert info.state == "paused"

        reconnected = Sandbox.connect(sandbox.sandbox_id)
        assert Sandbox.get_info(sandbox.sandbox_id).state == "running"
        result = reconnected.commands.run("echo after-resume")
        assert result.stdout == "after-resume\n"
        assert result.exit_code == 0
    finally:
        sandbox.kill()


def test_metrics(live_servers):
    sandbox = Sandbox.create()
    try:
        metrics = sandbox.get_metrics()
        assert len(metrics) >= 1
        assert metrics[0].cpu_count == 1
        assert metrics[0].mem_total == 1024 * 1024 * 1024
        assert metrics[0].disk_total > 0
        assert metrics[0].timestamp is not None
    finally:
        sandbox.kill()


@pytest.mark.asyncio
async def test_async_volume_and_secret(live_servers):
    from e2b import AsyncVolume

    volume = await AsyncVolume.create("async-volume")
    try:
        await volume.write_file("a.txt", b"async-data")
        assert await volume.read_file("a.txt") == "async-data"
    finally:
        await AsyncVolume.destroy(volume.volume_id)
