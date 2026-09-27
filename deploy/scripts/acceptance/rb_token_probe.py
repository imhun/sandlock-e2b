"""What actually leaks when the channel token travels in supervise's argv."""
import asyncio, os, subprocess, sys
from pathlib import Path
sys.path.insert(0, "/workspace")
from envd_service.route_b import W1SlotPool
from sandlock.supervise import SuperviseChannel

SLOT_UID, OTHER_UID = 21500, 21501
BASE = Path("/var/lib/e2b-sandboxes/_test-runtime/rb-token-probe")
POLICY = {
    "fs_readable": ["/usr", "/lib", "/lib64", "/bin", "/etc", "/proc", "/dev"],
    "fs_writable": [str(BASE)],
    "env": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
}
PY_SNIPPET = """
import sys
sys.path.insert(0, "/workspace")
from sandlock.supervise import SuperviseChannel
try:
    with SuperviseChannel(sys.argv[1], sys.argv[2]) as ch:
        print("VERB OK", ch.request("stats"))
except BaseException as e:
    print("RESULT", type(e).__name__, str(e)[:120])
"""


def as_uid(uid, *argv):
    cmd = ["setpriv", f"--reuid={uid}", f"--regid={uid}", "--clear-groups", "--", *argv]
    p = subprocess.run(cmd, capture_output=True, text=True)
    return (p.returncode, p.stdout + p.stderr)


async def main():
    BASE.mkdir(parents=True, exist_ok=True)
    os.chmod(BASE, 0o777)
    pool = W1SlotPool(uid_start=SLOT_UID, size=1, tmp_root=BASE / "slots")
    handle = await pool.acquire("sbx_token_probe", POLICY)
    pid, sock, token = handle.process.pid, str(handle.sock_path), handle.token
    print(f"slot pid={pid} uid={handle.uid} worker-uid-allowlist=[0] sock={sock}")

    print("\n--- 1/2. token in argv, and who can read it ----------------------")
    for uid in (0, SLOT_UID, OTHER_UID):
        rc, out = as_uid(uid, "sh", "-c", f"cat /proc/{pid}/cmdline 2>&1; true")
        text = out.replace("\0", " ")
        verdict = ("EPERM/ denied" if "Permission denied" in text or "not permitted" in text
                   else "READ" + (" + TOKEN VISIBLE" if token in text else " (token not found)"))
        print(f"  uid {uid:6d} /proc/{pid}/cmdline -> {verdict}")

    print("\n--- 3. socket reachability (registry is world-traversable) -------")
    st = Path(sock).parent.stat()
    print(f"  {Path(sock).parent} mode={oct(st.st_mode & 0o7777)}")
    rc, out = as_uid(OTHER_UID, "python3", "-c", PY_SNIPPET, sock, token)
    print(f"  uid {OTHER_UID} WITH the real token -> {out.strip()}")
    rc, out = as_uid(0, "python3", "-c", PY_SNIPPET, sock, "0" * 64)
    print(f"  uid 0 (allowlisted) wrong token   -> {out.strip()}")
    rc, out = as_uid(SLOT_UID, "python3", "-c", PY_SNIPPET, sock, token)
    print(f"  uid {SLOT_UID} (the sandbox itself)  -> {out.strip()}")

    print("\n--- 4. what a *connect* failure looks like to the client --------")
    try:
        SuperviseChannel("/tmp/definitely-not-a-slot.sock", "x")
    except BaseException as e:
        print(f"  missing socket -> {type(e).__name__}: {str(e)[:100]}")

    await pool.release("sbx_token_probe")
    print("\nslot released; pid gone =", not Path(f"/proc/{pid}").exists())

asyncio.run(main())
