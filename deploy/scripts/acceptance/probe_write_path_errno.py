"""N95: does the sandbox answer ENOENT for a write path whose parent is missing?

The contract every program relies on: `mkdir("<dir>/<new>")` where `<dir>` does
not exist must answer **ENOENT**, so the caller can walk up and create the
parent chain. Node's `fs.mkdir(p, {recursive: true})` and Go's
`os.MkdirAll` are both built that way -- on ENOENT they recurse into the parent
and retry, on anything else they give up.

This probe measures the errno the sandbox actually answers, side by side with
the two spellings that *are* expected to work (a direct child of the writable
root, and `mkdir -p` from the shell, which creates one level per syscall) and
with the read path, where the distinction is known to be kept.

Why it matters (measured 2026-10-10, `docs/n95-write-path-errno.md`):
`npm install -g <pkg>` cannot complete inside a sandbox -- npm's
`_createSparseTree` does one recursive `mkdir` per dependency directory, and
`bin-links` probes the bin path with `readlink`; both die on the collapsed
errno. The same shape would hit any installer that uses the standard
recursive-mkdir idiom.

Usage (against the local lane, or any deployment):

    python3 -u deploy/scripts/acceptance/probe_write_path_errno.py [--json]

Both URLs default to the local lane's control plane (`http://127.0.0.1:3110`) and
can be overridden through the environment. **Set both**: the create call goes to
`E2B_API_URL`, the command channel to `E2B_SANDBOX_URL`, and with only the first
one the SDK falls back to the create answer's `domain: localhost` -- every
command then dies with `tls handshake eof` on `49983-<id>.localhost` instead of
reaching the sandbox (measured 2026-10-10; the two runs that worked that day were
the two that had `E2B_SANDBOX_URL` exported).

The second read (`mkdir` under a missing parent) is the one this probe exists
for: **ENOENT is the correct answer; EACCES is the bug** (sandlock's chroot
write handler collapses "cannot resolve this path" into EACCES --
`crates/sandlock-core/src/chroot/dispatch.rs`, `read_and_resolve`).
"""

from __future__ import annotations

import json
import os
import sys

from e2b import Sandbox

#: The SDK needs both spellings of the same URL: create talks to the control
#: plane, commands go through the gateway. Defaulting both keeps this runnable
#: with no environment at all against the local lane.
API = os.environ.setdefault("E2B_API_URL", "http://127.0.0.1:3110")
os.environ.setdefault("E2B_SANDBOX_URL", API)
os.environ.setdefault("E2B_API_KEY", "local-key")

#: EACCES. The collapsed answer this probe is looking for.
EACCES = 13

#: The legs whose correct answer is ENOENT (2) -- "the parent is not there".
PARENT_MISSING_LEGS = ("mkdir 两层", "mkdir 三层", "symlink", "readlink")

#: One in-sandbox script, python stdlib only: the probe must run on the plain
#: `base` image, which has no node, no curl and no tar.
PROBE = r"""
import errno, json, os

readings = []


def leg(name, fn):
    try:
        fn()
        readings.append({"leg": name, "outcome": "ok", "errno": None, "code": None})
    except OSError as exc:
        readings.append(
            {
                "leg": name,
                "outcome": "errno",
                "errno": exc.errno,
                "code": errno.errorcode.get(exc.errno, str(exc.errno)),
            }
        )


leg("mkdir 直接子目录（父存在）", lambda: os.mkdir("/home/user/n95-a"))
leg("mkdir 两层（父不存在）", lambda: os.mkdir("/home/user/n95-b/inner"))
leg("mkdir 三层（父不存在）", lambda: os.mkdir("/home/user/n95-c/mid/leaf"))
leg("mkdir 写区外（/opt）", lambda: os.mkdir("/opt/n95-x"))
leg("symlink 目标父不存在", lambda: os.symlink("/etc/hostname", "/home/user/n95-d/sub/link"))
leg("readlink 父不存在", lambda: os.readlink("/home/user/n95-e/sub/link"))
leg("open 读不存在的文件（父存在）", lambda: open("/home/user/n95-a/absent.txt"))
leg("open 写（父存在）", lambda: open("/home/user/n95-a/ok.txt", "w").close())

print("PROBE_JSON " + json.dumps(readings))
"""

SHELL_LEG = 'mkdir -p /home/user/n95-f/one/two/three && echo "shell mkdir -p: ok"'


def main(argv: list[str]) -> int:
    as_json = "--json" in argv
    sb = Sandbox.create(timeout=600)
    say = (lambda *a, **k: None) if as_json else print
    say(f"sandbox {sb.sandbox_id}", flush=True)

    # The lane occasionally drops a single envd connection (`tls handshake eof`
    # on `49983-<id>.localhost`, measured 2026-10-10 twice in a row, gone on the
    # next run). One retry so a transport hiccup is not read as a finding.
    out = shell = None
    for attempt in (1, 2):
        try:
            out = sb.commands.run(f"python3 - <<'PY'\n{PROBE}\nPY", timeout=180).stdout
            shell = sb.commands.run(SHELL_LEG, timeout=60)
            break
        except Exception as exc:  # noqa: BLE001
            say(f"  attempt {attempt} failed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            if attempt == 2:
                sb.kill()
                raise
    sb.kill()

    line = next((l for l in out.splitlines() if l.startswith("PROBE_JSON ")), None)
    if line is None:
        print("probe produced no reading; raw output follows")
        print(out)
        return 1
    readings = json.loads(line[len("PROBE_JSON ") :])

    verdict = {
        "sandbox": sb.sandbox_id,
        "readings": readings,
        "shell_mkdir_p": shell.stdout.strip(),
        #: The bug: a *write* path under a missing parent answered EACCES.
        "collapsed": [
            r["leg"]
            for r in readings
            if r["leg"].startswith(PARENT_MISSING_LEGS) and r["errno"] == EACCES
        ],
    }
    if as_json:
        print(json.dumps(verdict, indent=2, ensure_ascii=False))
    else:
        for r in readings:
            print(f"  {r['leg']:<34} {r['outcome']:<6} {r['code'] or ''}")
        print(f"  {'shell mkdir -p':<34} {shell.stdout.strip()}")
        print()
        print(f"VERDICT collapsed-errno legs: {verdict['collapsed']}")
        print(
            "EXPECTED mkdir/symlink/readlink under a missing parent = ENOENT(2);"
            " EACCES(13) means the write handler collapsed the two."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
