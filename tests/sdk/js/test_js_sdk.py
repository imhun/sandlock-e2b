"""Run the official JS SDK (e2b@2.46.1) test suite against the live gateway.

Requires Node.js 20/22+ and ``npm install`` inside ``tests/sdk/js/``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent


_COPY_IGNORE = shutil.ignore_patterns("node_modules", "__pycache__")


@pytest.fixture(scope="session")
def js_workspace(tmp_path_factory: Path) -> Path:
    """Run the JS job on a copy, never inside the checkout.

    ``npm install`` writes platform-specific binaries (esbuild/rollup), so
    running it in ``tests/sdk/js`` would leave a Linux ``node_modules`` in a
    tree that is also used from macOS hosts -- and the reverse would break the
    container run. The scratch copy keeps both runners self-contained.
    """
    target = Path(tmp_path_factory.mktemp("sdk-js")) / "js"
    for entry in JS_DIR.iterdir():
        if entry.name in {"node_modules", "__pycache__", "test_js_sdk.py"}:
            continue
        destination = target / entry.name
        if entry.is_dir():
            shutil.copytree(entry, destination, ignore=_COPY_IGNORE)
        else:
            shutil.copy2(entry, destination)
    return target


def test_js_sdk(live_servers, js_workspace):
    if shutil.which("npm") is None:
        pytest.skip(
            "npm is not installed; run the JS SDK job separately "
            "(pnpm test --run tests/sdk/js)"
        )
    if not (js_workspace / "node_modules").is_dir():
        subprocess.run(["npm", "install"], cwd=js_workspace, check=True)
    env = dict(os.environ)
    env.update(
        {
            "E2B_API_URL": live_servers["api_url"],
            "E2B_SANDBOX_URL": live_servers["sandbox_url"],
            "E2B_API_KEY": "local-key",
        }
    )
    result = subprocess.run(
        ["npm", "test"],
        cwd=js_workspace,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"JS SDK tests failed:\n{result.stdout}\n{result.stderr}"
        )
