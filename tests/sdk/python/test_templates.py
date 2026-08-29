"""Local template build (v2.2): Dockerfile -> image -> sandbox template."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import httpx
import pytest

from e2b import Sandbox, Template


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _sandlock_active() -> bool:
    if sys.platform != "linux":
        return False
    mode = os.environ.get("E2B_EXECUTOR", "auto")
    if mode == "local":
        return False
    try:
        import sandlock  # noqa: F401

        return True
    except ImportError:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="template builds require the Docker daemon",
)


def test_template_build_and_create_sandbox(live_servers):
    template = Template().from_dockerfile(
        "FROM python:3.11-slim\nRUN echo template-built > /marker"
    )
    info = Template.build(template, "sdk-template")
    assert info.template_id.startswith("tpl_")
    assert info.build_id.startswith("bld_")
    assert info.name == "sdk-template"

    sandbox = Sandbox.create(template="sdk-template")
    try:
        assert sandbox.is_running() is True
        assert sandbox.commands.run("echo template-ok").stdout == "template-ok\n"
    finally:
        sandbox.kill()


def test_template_copy_context_build_succeeds(live_servers, tmp_path):
    """COPY build-context files are uploaded and the build completes."""
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("requests==2.32.0\n", encoding="utf-8")
    template = (
        Template(file_context_path=tmp_path)
        .from_image("python:3.11-slim")
        .copy("requirements.txt", "/app/")
    )
    info = Template.build(template, "sdk-copy-build")
    assert info.template_id.startswith("tpl_")
    assert info.build_id.startswith("bld_")


@pytest.mark.skipif(
    not _sandlock_active(),
    reason="image rootfs execution requires the Sandlock executor (Linux)",
)
def test_template_copy_file_visible_in_rootfs(multinode_servers):
    """With Sandlock, COPY'd files are visible inside the built image."""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as ctx:
        Path(ctx, "requirements.txt").write_text(
            "requests==2.32.0\n", encoding="utf-8"
        )
        template = (
            Template(file_context_path=ctx)
            .from_image("python:3.11-slim")
            .copy("requirements.txt", "/app/")
        )
        Template.build(
            template,
            "sdk-copy-rootfs",
            api_url=multinode_servers["api_url"],
            api_key="local-key",
        )
    sandbox = Sandbox.create(
        template="sdk-copy-rootfs",
        api_url=multinode_servers["api_url"],
        sandbox_url=multinode_servers["sandbox_url"],
        api_key="local-key",
    )
    try:
        result = sandbox.commands.run("cat /app/requirements.txt")
        assert result.stdout == "requests==2.32.0\n"
        assert result.exit_code == 0
    finally:
        sandbox.kill()


def test_template_build_pushes_to_registry(live_servers_registry):
    """With E2B_IMAGE_REGISTRY the built image is pushed after a successful
    build, so worker nodes can pull it instead of using the control plane's
    local daemon."""
    template = Template().from_dockerfile(
        "FROM python:3.11-slim\nRUN echo registry-built > /marker"
    )
    info = Template.build(
        template,
        "sdk-registry-push",
        api_url=live_servers_registry["api_url"],
        api_key="local-key",
    )
    catalog = httpx.get(
        f"http://{live_servers_registry['image_registry']}/v2/_catalog"
    ).json()
    assert info.template_id in catalog["repositories"]


def test_template_build_pushes_to_authenticated_registry(live_servers_registry_auth):
    """Registry credentials are used for docker login before pushing."""
    auth = live_servers_registry_auth["auth"]
    base = f"http://{auth['url']}"

    anonymous = httpx.get(f"{base}/v2/_catalog")
    assert anonymous.status_code == 401

    template = Template().from_dockerfile(
        "FROM python:3.11-slim\nRUN echo authed-built > /marker"
    )
    info = Template.build(
        template,
        "sdk-registry-auth-push",
        api_url=live_servers_registry_auth["api_url"],
        api_key="local-key",
    )
    catalog = httpx.get(
        f"{base}/v2/_catalog", auth=(auth["username"], auth["password"])
    ).json()
    assert info.template_id in catalog["repositories"]


@pytest.mark.skipif(
    not _sandlock_active(),
    reason="image rootfs execution requires the Sandlock executor (Linux)",
)
def test_template_registry_image_pulled_by_worker(multinode_servers_registry):
    """A worker without the local image tag pulls it from the registry and
    runs the sandbox inside the registry-hosted rootfs."""
    registry = multinode_servers_registry["image_registry"]
    template = Template().from_dockerfile(
        "FROM python:3.11-slim\nRUN echo registry-pulled > /marker"
    )
    info = Template.build(
        template,
        "sdk-registry-pull",
        api_url=multinode_servers_registry["api_url"],
        api_key="local-key",
    )
    remote = f"{registry}/{info.template_id}"
    # Remove the local tag so the worker must fetch the image from the
    # registry (the layer blob may remain cached, but the image reference
    # resolution exercises the pull path).
    removed = subprocess.run(
        ["docker", "rmi", "-f", remote], capture_output=True
    )
    assert removed.returncode == 0

    sandbox = Sandbox.create(
        template="sdk-registry-pull",
        api_url=multinode_servers_registry["api_url"],
        sandbox_url=multinode_servers_registry["sandbox_url"],
        api_key="local-key",
    )
    try:
        result = sandbox.commands.run("cat /marker")
        assert result.stdout == "registry-pulled\n"
        assert result.exit_code == 0
    finally:
        sandbox.kill()


@pytest.mark.skipif(
    not _sandlock_active(),
    reason="image rootfs execution requires the Sandlock executor (Linux)",
)
def test_template_registry_auth_image_pulled_by_worker(
    multinode_servers_registry_auth,
):
    """Workers log into the authenticated registry before pulling."""
    registry = multinode_servers_registry_auth["image_registry"]
    template = Template().from_dockerfile(
        "FROM python:3.11-slim\nRUN echo authed-pulled > /marker"
    )
    info = Template.build(
        template,
        "sdk-registry-auth-pull",
        api_url=multinode_servers_registry_auth["api_url"],
        api_key="local-key",
    )
    remote = f"{registry}/{info.template_id}"
    removed = subprocess.run(["docker", "rmi", "-f", remote], capture_output=True)
    assert removed.returncode == 0

    sandbox = Sandbox.create(
        template="sdk-registry-auth-pull",
        api_url=multinode_servers_registry_auth["api_url"],
        sandbox_url=multinode_servers_registry_auth["sandbox_url"],
        api_key="local-key",
    )
    try:
        result = sandbox.commands.run("cat /marker")
        assert result.stdout == "authed-pulled\n"
        assert result.exit_code == 0
    finally:
        sandbox.kill()


@pytest.mark.skipif(
    not _sandlock_active(),
    reason="image rootfs execution requires the Sandlock executor (Linux)",
)
def test_template_image_rootfs_marker(multinode_servers):
    """With Sandlock, the sandbox runs inside the built image rootfs."""
    template = Template().from_dockerfile(
        "FROM python:3.11-slim\nRUN echo template-built > /marker"
    )
    Template.build(
        template,
        "sdk-template-rootfs",
        api_url=multinode_servers["api_url"],
        api_key="local-key",
    )
    sandbox = Sandbox.create(
        template="sdk-template-rootfs",
        api_url=multinode_servers["api_url"],
        sandbox_url=multinode_servers["sandbox_url"],
        api_key="local-key",
    )
    try:
        result = sandbox.commands.run("cat /marker")
        assert result.stdout == "template-built\n"
        assert result.exit_code == 0
    finally:
        sandbox.kill()


@pytest.mark.asyncio
async def test_async_template_build(live_servers):
    from e2b import AsyncSandbox
    from e2b import AsyncTemplate

    template = AsyncTemplate().from_dockerfile(
        "FROM python:3.11-slim\nRUN echo async-built"
    )
    info = await AsyncTemplate.build(template, "sdk-async-template")
    assert info.template_id.startswith("tpl_")
    sandbox = await AsyncSandbox.create(template="sdk-async-template")
    try:
        assert await sandbox.is_running() is True
    finally:
        await sandbox.kill()
