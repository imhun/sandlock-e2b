"""E5.1: the worker/supervisor container must run as a non-root user.

Builds ``deploy/docker/Dockerfile.envd`` (the fork sandlock wheel comes from
``wheels/fork/``) and verifies from inside the container:

* the image's default USER is uid 65534;
* with the production security shape (seccomp unconfined, NET_ADMIN, host
  network, low-port sysctl) a non-root supervisor can still create plain,
  network-enabled and image-rootfs (chroot) sandboxes.

The rootfs for the chroot probe is exported from the freshly built worker
image itself, so the test needs no registry access and is deterministic.

Requires Linux with a Docker daemon and the prebuilt fork wheels
(``wheels/fork/*.whl``); skipped elsewhere.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
WORKER_IMAGE = "e2b-sandlock-worker-nonroot:e5.1-test"
WORKER_UID = 65534


def _worker_wheels_present() -> bool:
    wheels = PROJECT_ROOT / "wheels" / "fork"
    return wheels.is_dir() and any(wheels.glob("*.whl"))


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    result = subprocess.run(
        ["docker", "info"], capture_output=True, text=True
    )
    return result.returncode == 0


def _bind_root() -> Path:
    """Host-side path of this test's repository mount (for docker -v).

    Inside the test-runner container the repo lives at /workspace while the
    Docker daemon sees the bind source (e.g. /Users/.../sandlock-e2b). When
    the tests run directly on the docker host, the paths already agree.
    """
    try:
        listed = subprocess.run(
            ["docker", "ps", "-q"], capture_output=True, text=True, check=True
        ).stdout
        for cid in listed.split():
            inspect = subprocess.run(
                ["docker", "inspect", cid],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
            mounts = json.loads(inspect)[0].get("Mounts", [])
            for mount in mounts:
                source = mount.get("Source") or ""
                if (
                    mount.get("Destination") == str(PROJECT_ROOT)
                    and "sandlock-e2b" in source
                    and source != str(PROJECT_ROOT)
                ):
                    return Path(source)
    except Exception:
        pass
    return PROJECT_ROOT


pytestmark = pytest.mark.skipif(
    sys.platform != "linux"
    or not _docker_ready()
    or not _worker_wheels_present(),
    reason=(
        "E5.1 container tests need Linux, a reachable Docker daemon "
        "(mount /var/run/docker.sock) and the fork wheels "
        "(run ./deploy/scripts/build-sandlock-wheels.sh first)"
    ),
)


def _run(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args), capture_output=True, text=True, **kwargs
    )


def _build_worker_image(build_ctx: Path) -> None:
    """Build the worker image from a minimal context (hardlink copies).

    The repo root context is gigabytes (third_party, tmp_pytest), so only
    the paths the Dockerfile consumes are copied into a temporary context.
    """
    dockerfile = build_ctx / "Dockerfile"
    shutil.copy2(
        PROJECT_ROOT / "deploy" / "docker" / "Dockerfile.envd", dockerfile
    )
    for name in ("requirements.txt",):
        shutil.copy2(PROJECT_ROOT / name, build_ctx / name)
    for name in ("gateway_common", "envd_service"):
        src = PROJECT_ROOT / name
        dst = build_ctx / name
        shutil.copytree(src, dst, symlinks=True)
    # Track F (F1): the worker Dockerfile also compiles the two
    # file-capability brokers from deploy/priv/, so the minimal context has to
    # carry that directory too (a missing input fails the COPY, not the
    # build's ability to run the image).
    shutil.copytree(
        PROJECT_ROOT / "deploy" / "priv", build_ctx / "deploy" / "priv",
        symlinks=True,
    )
    arch = platform.machine()
    if arch == "x86_64":
        target_arch = "amd64"
        wheel_arch = "x86_64"
    elif arch == "aarch64":
        target_arch = "arm64"
        wheel_arch = "aarch64"
    else:
        raise RuntimeError(f"unsupported host architecture for image build: {arch}")
    wheels_dst = build_ctx / "wheels" / "fork"
    wheels_dst.mkdir(parents=True)
    # Only the host-arch wheel is copied: the legacy builder does not inject
    # TARGETARCH, and the Dockerfile's fallback pick is alphabetical.
    for wheel in (PROJECT_ROOT / "wheels" / "fork").glob(f"*{wheel_arch}.whl"):
        shutil.copy2(wheel, wheels_dst / wheel.name)
    result = _run(
        "docker",
        "build",
        "--pull=false",
        "--build-arg",
        f"TARGETARCH={target_arch}",
        "-t",
        WORKER_IMAGE,
        str(build_ctx),
    )
    assert result.returncode == 0, (
        f"docker build failed:\n{result.stdout}\n{result.stderr}"
    )


@pytest.fixture(scope="module")
def worker_image(tmp_path_factory) -> str:
    _build_worker_image(tmp_path_factory.mktemp("worker-nonroot-ctx"))
    return WORKER_IMAGE


def test_worker_image_default_user_is_nonroot(worker_image) -> None:
    result = _run(
        "docker", "run", "--rm", "--entrypoint", "id", worker_image, "-u"
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(WORKER_UID)


def _export_rootfs(image: str, dest: Path) -> None:
    created = _run("docker", "create", image)
    assert created.returncode == 0, created.stderr
    container_id = created.stdout.strip()
    try:
        export = subprocess.run(
            ["docker", "export", container_id],
            capture_output=True,
        )
        assert export.returncode == 0, export.stderr.decode()
        untar = subprocess.run(
            ["tar", "-x", "-C", str(dest)],
            input=export.stdout,
            capture_output=True,
        )
        assert untar.returncode == 0, untar.stderr.decode()
    finally:
        _run("docker", "rm", "-f", container_id)


def _chown_all(path: Path, uid: int, gid: int) -> None:
    if os.geteuid() == 0:
        subprocess.run(
            ["chown", "-R", f"{uid}:{gid}", str(path)], check=True
        )
    else:
        os.chmod(path, 0o777)


def test_worker_nonroot_sandbox_network_rootfs_all_green(worker_image) -> None:
    bind_root = _bind_root()
    # Project-local tmp dir (spec: temp data in tmp/): it is visible to the
    # Docker daemon through the same bind mount, so the probe container can
    # mount it regardless of E2B_TEST_TMP_ROOT.
    probe_root = PROJECT_ROOT / "tmp" / f"e5-nonroot-{uuid.uuid4().hex[:12]}"
    probe_ws = probe_root / "probe-ws"
    probe_ws.mkdir(parents=True)
    rootfs = probe_root / "rootfs"
    rootfs.mkdir(parents=True)
    try:
        _export_rootfs(worker_image, rootfs)
        _chown_all(rootfs, WORKER_UID, WORKER_UID)
        _chown_all(probe_ws, WORKER_UID, WORKER_UID)
        probe_src = bind_root / probe_ws.relative_to(PROJECT_ROOT)
        rootfs_src = bind_root / rootfs.relative_to(PROJECT_ROOT)
        repo_src = bind_root

        result = _run(
            "docker",
            "run",
            "--rm",
            "--privileged",
            "--security-opt",
            "seccomp=unconfined",
            "--cap-add",
            "NET_ADMIN",
            "--network",
            "host",
            "--user",
            f"{WORKER_UID}:{WORKER_UID}",
            "-v",
            f"{repo_src}:/workspace:ro",
            "-v",
            f"{probe_src}:/probe",
            "-v",
            f"{rootfs_src}:/rootfs",
            "-w",
            "/workspace",
            "-e",
            "PYTHONPATH=/workspace",
            worker_image,
            "python",
            "/workspace/tests/security/worker_nonroot_probe.py",
            str(WORKER_UID),
            "/probe",
            "/rootfs",
        )
        assert result.returncode == 0, (
            f"non-root worker probe failed (exit {result.returncode}):\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        assert "PASS plain sandbox" in result.stdout
        assert "PASS network sandbox" in result.stdout
        assert "PASS rootfs sandbox" in result.stdout
    finally:
        shutil.rmtree(probe_root, ignore_errors=True)
