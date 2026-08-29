"""Shared volumes: volume data on shared storage is mountable on remote
worker nodes, so volume-bearing sandboxes schedule onto any node."""

from __future__ import annotations

import os

import pytest

from e2b import Sandbox, Volume


def _opts(servers):
    return {
        "api_url": servers["api_url"],
        "sandbox_url": servers["sandbox_url"],
        "api_key": "local-key",
    }


def _volume_opts(servers):
    return {
        "api_url": servers["api_url"],
        "api_key": "local-key",
    }


def test_shared_volume_mount_on_remote_node(multinode_servers):
    os.environ["E2B_VOLUME_API_URL"] = multinode_servers["api_url"]
    try:
        volume = Volume.create("shared-volume", **_volume_opts(multinode_servers))
        try:
            volume.write_file("data.txt", b"shared-data")
            sandbox = Sandbox.create(
                volume_mounts={"mnt/data": volume.volume_id}, **_opts(multinode_servers)
            )
            try:
                assert sandbox.is_running() is True
                result = sandbox.commands.run("cat mnt/data/data.txt")
                assert result.stdout == "shared-data"
                assert result.exit_code == 0

                # Writes through the sandbox persist into the shared volume.
                assert sandbox.commands.run(
                    "echo sandbox-write > mnt/data/from-sandbox.txt"
                ).exit_code == 0
                assert volume.read_file("from-sandbox.txt") == "sandbox-write\n"
            finally:
                sandbox.kill()

            # The volume survives the sandbox and is visible from a new one.
            second = Sandbox.create(
                volume_mounts={"mnt/data": volume.volume_id}, **_opts(multinode_servers)
            )
            try:
                result = second.commands.run("cat mnt/data/from-sandbox.txt")
                assert result.stdout == "sandbox-write\n"
            finally:
                second.kill()
        finally:
            Volume.destroy(volume.volume_id, **_volume_opts(multinode_servers))
    finally:
        os.environ.pop("E2B_VOLUME_API_URL", None)


def test_shared_volume_sandbox_cannot_reach_other_volumes(multinode_servers):
    """Only the explicitly mounted volume directory is accessible; sibling
    volumes under the shared root are not."""
    if not _sandlock_available():
        pytest.skip(
            "filesystem isolation is enforced by Sandlock (Linux test runner)"
        )
    os.environ["E2B_VOLUME_API_URL"] = multinode_servers["api_url"]
    try:
        mounted = Volume.create("mounted-vol", **_volume_opts(multinode_servers))
        sibling = Volume.create("sibling-vol", **_volume_opts(multinode_servers))
        try:
            mounted.write_file("own.txt", b"own-data")
            sibling.write_file("secret.txt", b"top-secret")
            sandbox = Sandbox.create(
                volume_mounts={"mnt/data": mounted.volume_id},
                **_opts(multinode_servers),
            )
            try:
                assert sandbox.commands.run("cat mnt/data/own.txt").stdout == "own-data"
                # The sibling volume is a different directory under the shared
                # root; it must not be reachable from the sandbox.
                from e2b.sandbox.commands.command_handle import CommandExitException

                with pytest.raises(CommandExitException) as exc:
                    sandbox.commands.run(
                        "cat mnt/data/../sibling-vol/secret.txt"
                    )
                assert "top-secret" not in exc.value.stdout
            finally:
                sandbox.kill()
        finally:
            Volume.destroy(mounted.volume_id, **_volume_opts(multinode_servers))
            Volume.destroy(sibling.volume_id, **_volume_opts(multinode_servers))
    finally:
        os.environ.pop("E2B_VOLUME_API_URL", None)


def _sandlock_available() -> bool:
    import shutil
    import sys

    if sys.platform != "linux":
        return False
    if os.environ.get("E2B_EXECUTOR") == "local":
        return False
    try:
        import sandlock  # noqa: F401

        return True
    except ImportError:
        return False
