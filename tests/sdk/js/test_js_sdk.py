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


def test_js_sdk(live_servers):
    if shutil.which("npm") is None:
        pytest.skip("npm is not installed; run the JS SDK job separately (pnpm test --run tests/sdk/js)")
    if not (JS_DIR / "node_modules").is_dir():
        subprocess.run(["npm", "install"], cwd=JS_DIR, check=True)
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
        cwd=JS_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"JS SDK tests failed:\n{result.stdout}\n{result.stderr}"
        )
