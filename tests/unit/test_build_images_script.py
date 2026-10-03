"""N72: ``build-images.sh`` 单平台也必须听 ``PUSH=1``。

2026-10-02 上线踩到的形状：``PLATFORMS=linux/arm64``（不带逗号）走单平台分支，
而 ``PUSH=1`` 只在**多平台**分支被检查/使用 —— 于是 worker / agent /
quota-agent 三个镜像本地 ``--load`` 完就结束了，**没进 ACR**；只有
``control-plane-gateway``（走 ``--push``）上去了。本轮是手工 ``docker push``
补的。命令矩阵现在写死在脚本头注释里，这里把它逐格钉成行为：

* ``PLATFORMS=linux/arm64 PUSH=1``            -> 三次 ``--push``，不含 ``--load``
* ``PLATFORMS=linux/arm64``（无 ``PUSH``）    -> 三次 ``--load``，不含 ``--push``
* ``PLATFORMS=linux/amd64,linux/arm64``       -> 必须 ``PUSH=1``，否则非零退出

真跑脚本，但把 ``docker`` 换成 PATH 最前面的 stub：stub 记录每一条 argv 然后
返回 0，所以既没有任何真实构建，也没有任何推送。stub 记录的是逐参数的 JSON
数组，断言是逐字/逐参数的（不是 ``in`` 子串）。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "deploy" / "scripts" / "build-images.sh"

#: Deterministic inputs, so the recorded tags are exact strings.
REGISTRY = "registry.example.com/e2b-test"
VERSION = "9.9.9-test"

#: The three images the script builds, in the order it builds them.
IMAGES = ("worker", "agent", "quota-agent")

MULTI_PLATFORM = "linux/amd64,linux/arm64"
MULTI_PLATFORM_REFUSAL = (
    "multi-platform builds require PUSH=1 (output goes to a registry)\n"
)

STUB_DOCKER = '''#!/usr/bin/env python3
"""Fake docker: record every argv, succeed at everything.

The script under test only ever calls `docker buildx build`, but this stub
answers `inspect`/`create`/`use` with 0 as well -- the point is that no real
build or push can run, not that the fake is narrow.
"""
import json
import os
import sys
from pathlib import Path

root = Path(os.environ["STUB_ROOT"])
with (root / "docker.log").open("a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
raise SystemExit(0)
'''


def _stub_env(tmp_path: Path, *, platforms: str, push: str | None) -> dict[str, str]:
    """A stub ``docker`` on ``PATH`` (the only one reachable) + the argv log."""
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "docker"
    stub.write_text(STUB_DOCKER, encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}{os.pathsep}{env['PATH']}"
    env["STUB_ROOT"] = str(tmp_path)
    env["REGISTRY"] = REGISTRY
    env["VERSION"] = VERSION
    env["PLATFORMS"] = platforms
    # The case decides whether PUSH exists at all; an inherited PUSH must not
    # leak in and make the "no PUSH" rows lie.
    env.pop("PUSH", None)
    if push is not None:
        env["PUSH"] = push
    return env


def _stub_is_the_docker(env: dict[str, str]) -> None:
    """The stub must be the ``docker`` that resolves on this ``PATH``."""
    bindir = env["PATH"].split(os.pathsep)[0]
    assert shutil.which("docker", path=env["PATH"]) == str(Path(bindir) / "docker")


def _run(
    tmp_path: Path, *, platforms: str, push: str | None
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    env = _stub_env(tmp_path, platforms=platforms, push=push)
    _stub_is_the_docker(env)
    result = subprocess.run(
        [str(SCRIPT)],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    log = tmp_path / "docker.log"
    recorded = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    return result, recorded


def _builds(recorded: list[list[str]]) -> list[list[str]]:
    """Only the ``docker buildx build ...`` calls, in order."""
    return [argv for argv in recorded if argv[:2] == ["buildx", "build"]]


def _image_tags(builds: list[list[str]]) -> list[str]:
    return [argv[argv.index("-t") + 1] for argv in builds]


def test_single_platform_pushes_when_push_is_one(tmp_path: Path) -> None:
    """The bug itself: single platform + ``PUSH=1`` must reach the registry."""
    result, recorded = _run(tmp_path, platforms="linux/arm64", push="1")

    assert result.returncode == 0, result.stdout + result.stderr
    builds = _builds(recorded)
    assert len(builds) == 3
    # Every build goes to the registry, and none of them is a local load.
    assert [argv[2] for argv in builds] == ["--push", "--push", "--push"]
    assert all("--load" not in argv for argv in builds)
    assert [argv[argv.index("--platform") + 1] for argv in builds] == [
        "linux/arm64"
    ] * 3
    assert _image_tags(builds) == [
        f"{REGISTRY}/e2b-sandlock-{image}:{VERSION}" for image in IMAGES
    ]


def test_single_platform_loads_without_push(tmp_path: Path) -> None:
    """No ``PUSH`` keeps the old local behaviour: ``--load``, never ``--push``."""
    result, recorded = _run(tmp_path, platforms="linux/arm64", push=None)

    assert result.returncode == 0, result.stdout + result.stderr
    builds = _builds(recorded)
    assert len(builds) == 3
    assert [argv[2] for argv in builds] == ["--load", "--load", "--load"]
    assert all("--push" not in argv for argv in builds)
    assert _image_tags(builds) == [
        f"{REGISTRY}/e2b-sandlock-{image}:{VERSION}" for image in IMAGES
    ]


def test_multi_platform_requires_push(tmp_path: Path) -> None:
    """The existing discipline, pinned so the single-platform fix cannot drop it."""
    result, recorded = _run(tmp_path, platforms=MULTI_PLATFORM, push=None)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == MULTI_PLATFORM_REFUSAL
    # Refused *before* anything was built: no docker call at all.
    assert recorded == []


def test_multi_platform_pushes_when_push_is_one(tmp_path: Path) -> None:
    """Multi platform + ``PUSH=1`` still goes to the registry."""
    result, recorded = _run(tmp_path, platforms=MULTI_PLATFORM, push="1")

    assert result.returncode == 0, result.stdout + result.stderr
    builds = _builds(recorded)
    assert len(builds) == 3
    assert [argv[2] for argv in builds] == ["--push", "--push", "--push"]
    assert all("--load" not in argv for argv in builds)
    assert [argv[argv.index("--platform") + 1] for argv in builds] == [
        MULTI_PLATFORM
    ] * 3
