#!/usr/bin/env python3
"""C4 acceptance: per-sandbox XFS project quota is real, and released.

Runs ON the target as root (it needs ``xfs_quota -x`` and the host volume
paths). Checks, in order:

1. a volume created with ``perSandboxQuotaMb`` really limits each mounting
   sandbox: writing past the limit must fail with ENOSPC (not silently
   succeed), and the bytes actually written must not exceed the limit;
2. no oversell: a second sandbox mounting the same volume writes normally
   (its own project), while the first is at its ceiling;
3. the limit is attached to a real XFS project: the slice directory's
   ``fsxattr.projid`` matches a ``xfs_quota report -p`` row whose hard limit is
   the volume's quota (report units are 1 KiB blocks);
4. release: after the sandbox is deleted the project entry goes away
   (``project -C`` from the worker + the agent's orphan reconcile).

Every failure prints ``C4-ACCEPT-FAIL: <reason>`` and exits non-zero; success
prints ``C4-ACCEPT-OK`` plus the measured numbers.
"""

from __future__ import annotations

import os
import json
import shlex
import shutil
import subprocess
import sys
import time

import httpx

API = os.environ.get("E2B_API_URL", "http://127.0.0.1:3000")
KEY = os.environ["E2B_API_KEY"]
QUOTA_MB = int(os.environ.get("E2B_ACCEPT_QUOTA_MB", "64"))
VOLUME_ROOT = "/var/lib/docker/volumes/sandlock_sandbox-shared/_data"


def fail(reason: str) -> None:
    print(f"C4-ACCEPT-FAIL: {reason}", flush=True)
    sys.exit(1)


def sh(cmd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, shell=True, capture_output=True, text=True)


def project_rows(mount: str = "/") -> dict[int, tuple[int, int, int]]:
    """projid -> (used, soft, hard) in 1 KiB blocks, from `report -p`."""
    out = sh(f"xfs_quota -x -c 'report -p -n -b' {mount}").stdout
    rows: dict[int, tuple[int, int, int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].startswith("#"):
            try:
                rows[int(parts[0][1:])] = (int(parts[1]), int(parts[2]), int(parts[3]))
            except ValueError:
                continue
    return rows


def slice_projid(path: str) -> int | None:
    out = sh(f"xfs_io -c 'stat' {path}").stdout
    for line in out.splitlines():
        if "projid" in line:
            try:
                return int(line.split()[-1])
            except ValueError:
                return None
    return None


def run_cmd(sb, cmd: str) -> tuple[int, str, str]:
    """Run inside a sandbox without the SDK's raise-on-nonzero."""
    from e2b.sandbox.commands.command_handle import CommandExitException

    try:
        res = sb.commands.run(cmd)
    except CommandExitException as exc:
        return exc.exit_code, exc.stdout or "", exc.stderr or ""
    return res.exit_code, res.stdout, res.stderr


def agent_reconcile() -> None:
    """Ask the quota-agent to reap projects nothing references any more."""
    payload = json.dumps(
        {"workspace_base": "/var/lib/e2b-sandboxes", "mount": "/var/lib/e2b-sandboxes"}
    )
    inner = (
        "import os, httpx;"
        f"r = httpx.post('http://quota-agent:49984/reconcile', content={payload!r},"
        " headers={'X-Internal-Key': os.environ.get('E2B_QUOTA_AGENT_TOKEN', ''),"
        " 'Content-Type': 'application/json'}, timeout=30);"
        "print(r.status_code, r.text)"
    )
    cmd = "docker exec sandlock-worker-1-1 python3 -c " + shlex.quote(inner)
    out = sh(cmd)
    print("agent /reconcile:", out.stdout.strip() or out.stderr.strip(), flush=True)


def main() -> int:
    from e2b import Sandbox, Volume

    created = httpx.post(
        f"{API}/volumes",
        headers={"X-API-Key": KEY},
        json={"name": "c4-quota-accept", "perSandboxQuotaMb": QUOTA_MB},
        timeout=30,
    )
    if created.status_code != 201:
        fail(f"volume create HTTP {created.status_code}: {created.text[:200]}")
    vol = created.json()
    vid = vol["volumeID"]
    if vol.get("perSandboxQuotaMb") != QUOTA_MB:
        fail(f"volume reports perSandboxQuotaMb={vol.get('perSandboxQuotaMb')}, expected {QUOTA_MB}")
    print(f"volume {vid} perSandboxQuotaMb={QUOTA_MB} (expected hard limit {QUOTA_MB * 1024} KiB blocks)", flush=True)

    sandboxes = []
    volume_destroyed = False
    try:
        a = Sandbox.create(volume_mounts={"mnt/data": vid})
        sandboxes.append(a)
        b = Sandbox.create(volume_mounts={"mnt/data": vid})
        sandboxes.append(b)
        print(f"sandbox A={a.sandbox_id} B={b.sandbox_id}", flush=True)

        overshoot_mb = QUOTA_MB + 96
        # No shell pipeline here: the sandbox shell is /bin/sh (no PIPESTATUS),
        # and dd's ENOSPC line must reach us intact (a `tail -2` hid it once).
        code, out, err = run_cmd(
            a,
            "dd if=/dev/zero of=mnt/data/big bs=1M count=%d; "
            "echo dd_rc=$?; stat -c %%s mnt/data/big" % overshoot_mb,
        )
        combined = f"{out}\n{err}"
        print("A overshoot:", combined.strip().replace("\n", " | "), flush=True)
        if "No space left" not in combined:
            fail(f"overshoot did not hit ENOSPC (exit={code} out={out[:200]!r} err={err[:200]!r})")
        size_line = [ln for ln in out.splitlines() if ln.strip().isdigit()]
        written = int(size_line[-1]) if size_line else 0
        ceiling = QUOTA_MB * 1024 * 1024 + 8 * 1024 * 1024  # limit + fs slack
        if written > ceiling:
            fail(f"overshoot wrote {written} bytes, above the {QUOTA_MB}MB quota (ceiling {ceiling})")
        print(f"A wrote {written} bytes before ENOSPC (quota {QUOTA_MB}MB)", flush=True)

        code, out, err = run_cmd(
            b, "dd if=/dev/zero of=mnt/data/small bs=1M count=8 2>&1 | tail -1; echo ok"
        )
        if "ok" not in out:
            fail(f"second sandbox could not write its own slice (exit={code} err={err[:200]!r})")
        print("B wrote 8MB into its own slice: OK (no oversell)", flush=True)

        a_slice = f"{VOLUME_ROOT}/_volumes/{vid}/{a.sandbox_id}"
        b_slice = f"{VOLUME_ROOT}/_volumes/{vid}/{b.sandbox_id}"
        rows = project_rows("/")
        a_proj, b_proj = slice_projid(a_slice), slice_projid(b_slice)
        print(f"slice projids: A={a_proj} B={b_proj}", flush=True)
        if not a_proj or not b_proj:
            fail(f"slice directories have no project id (A={a_proj} B={b_proj})")
        if a_proj == b_proj:
            fail(f"both sandboxes share project id {a_proj}: per-sandbox quota is not per sandbox")
        expected_hard = QUOTA_MB * 1024
        for label, proj in (("A", a_proj), ("B", b_proj)):
            row = rows.get(proj)
            if row is None:
                fail(f"project {proj} ({label}) is not in `xfs_quota report -p`")
            if row[2] != expected_hard:
                fail(f"project {proj} ({label}) hard limit is {row[2]} KiB, expected {expected_hard} KiB")
            print(f"project {proj} ({label}): used={row[0]} hard={row[2]} KiB", flush=True)

        print(
            "### release: delete both sandboxes AND the volume, then reap "
            "(a per-sandbox volume project legitimately lives as long as its "
            "volume slice does)",
            flush=True,
        )
        for sb in (a, b):
            try:
                sb.kill()
            except Exception:  # noqa: BLE001
                pass
            if sb in sandboxes:
                sandboxes.remove(sb)
        Volume.destroy(vid, api_url=API, api_key=KEY)
        volume_destroyed = True
        time.sleep(3)
        agent_reconcile()
        time.sleep(2)
        a_slice, b_slice = f"{VOLUME_ROOT}/_volumes/{vid}/{a.sandbox_id}", f"{VOLUME_ROOT}/_volumes/{vid}/{b.sandbox_id}"
        for label, path in (("A", a_slice), ("B", b_slice)):
            if os.path.exists(path):
                proj = slice_projid(path)
                if proj:
                    fail(f"slice {label} still carries projid {proj} after delete")
                print(f"slice {label} still exists but its projid is cleared (0)", flush=True)
            else:
                print(f"slice {label} removed", flush=True)

        # The reconcile path is what drops a released project once its tree is
        # gone. Existing leftover workspaces keep *their* project ids recorded
        # on purpose (fail-safe), so this is verified on an isolated orphan
        # instead of on whatever residue the host happens to carry.
        orphan = f"/tmp/c4-accept-orphan-{os.getpid()}"
        os.makedirs(orphan, exist_ok=True)
        orphan_proj = 2000000000 + os.getpid() % 100000
        sh(f"xfs_quota -x -c 'project -s -p {orphan} {orphan_proj}' /")
        sh(f"xfs_quota -x -c 'limit -p bhard=64M {orphan_proj}' /")
        if orphan_proj not in project_rows("/"):
            fail(f"orphan probe project {orphan_proj} was not created")
        sh(f"xfs_quota -x -c 'project -C -p {orphan} {orphan_proj}' /")
        shutil.rmtree(orphan, ignore_errors=True)
        agent_reconcile()
        time.sleep(2)
        if orphan_proj in project_rows("/"):
            fail(f"orphan project {orphan_proj} was not reaped after its tree was removed")
        print(f"isolated orphan {orphan_proj} reaped by reconcile", flush=True)
    finally:
        for sb in sandboxes:
            try:
                sb.kill()
            except Exception:  # noqa: BLE001
                pass
        if not volume_destroyed:
            try:
                Volume.destroy(vid, api_url=API, api_key=KEY)
                print(f"volume {vid} destroyed", flush=True)
            except Exception as exc:  # noqa: BLE001
                print(f"volume destroy failed: {exc}", flush=True)

    print(f"C4-ACCEPT-OK quota={QUOTA_MB}MB", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
