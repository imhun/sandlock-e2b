"""Does the standalone probe's stdout loss depend on the client's fd layout?

The scratch harness (`tmp/f11_fup3_probe.py`) loses every sandbox stdout write
(exit 120 / empty stdout, and even in-sandbox file creation fails) when it is
run as a bare python script, while the in-repo contract for the same five
outcomes is green in both shapes.  Both drive the harness in-process, so the
remaining difference is the *client process* itself: a bare script holds a
handful of descriptors, pytest holds many.  Open N extra descriptors before
starting the same probe and report what the sandbox command returns.
"""
from __future__ import annotations

import os
import runpy
import sys

N = int(sys.argv[1]) if len(sys.argv) > 1 else 64
held = [os.open("/dev/null", os.O_RDONLY) for _ in range(N)]
print(f"holding {len(held)} extra fds; highest={max(held) if held else -1}", flush=True)
sys.argv = ["f11_fup3_probe.py"]
runpy.run_path("/workspace/tmp/f11_fup3_probe.py", run_name="__main__")
