"""N27 Task 6: the one-shot state-base migration is pinned by its contract.

The migration is the one step of N27 that *moves* a tenant's data. It runs
once, by hand, on a cluster, with the workers stopped -- so it is pinned twice:

* **statically**, on the script's and the Job's text: the properties a reviewer
  has to re-read after any rewrite. Dry-run by default, every move a
  `rename(2)`, no recursive delete anywhere, the worker=0 gate, `umask 077`
  plus an explicit `0600` on the one file that outlives the run, and a Job that
  runs as root on the shared PVC with no automatic retry;
* **behaviourally**, by running the script offline (`--root`) over a synthetic
  export under `tmp/` and checking the bytes: a tree keeps its inode, the old
  platform directories are left where they are, the rollback restores the
  shape. A poisoned `kubectl` on `PATH` turns "the offline path never talks to
  a cluster" into an assertion instead of a promise.

Script lines are matched exactly (stripped), the way
`test_worker_manifest_permissions.py` pins the worker's init script: a
rewritten line has to be re-read here rather than pass on a substring of the
old one.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "deploy" / "scripts" / "migrate-state-base.sh"
JOB = REPO / "deploy" / "k8s-k0s" / "state-base-migrate.yaml"
KUSTOMIZATION = REPO / "deploy" / "k8s-k0s" / "kustomization.yaml"
VERSION_FILE = REPO / "deploy" / "stack" / ".version"

#: The export root's platform namespaces that stay put: they hang off the
#: *export* root the deployment names (`E2B_SHARED_WORKSPACE_ROOT`), and only
#: the sandbox trees sink one level.
STAY_AT_EXPORT_ROOT = (
    "_builds",
    "_images",
    "_secrets",
    "_templates",
    "_snapshots",
    "_volumes",
)

#: The new layout the migration produces.
STATE_DIR = "state"
TREES_DIR = "workspaces"
JOURNAL_REL = f"{STATE_DIR}/.state-base-migration.journal"


def _lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]


def _embedded(marker: str, path: Path) -> list[str]:
    """The stripped lines of the `<<'MARKER'` heredoc inside `path`."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = next(index for index, line in enumerate(lines) if line.strip().endswith(f"<<'{marker}'"))
    end = next(index for index in range(start + 1, len(lines)) if lines[index].strip() == marker)
    return [line.strip() for line in lines[start + 1 : end]]


def _counts(path: Path) -> tuple[int, int, int]:
    """(files, directories, bytes) under `path`, recursively."""
    files = dirs = nbytes = 0
    for cur, subdirs, names in os.walk(path):
        dirs += len(subdirs)
        for name in names:
            st = os.lstat(Path(cur) / name)
            files += 1
            if stat.S_ISREG(st.st_mode):
                nbytes += st.st_size
    return files, dirs, nbytes


def _inventory(root: Path) -> dict[str, tuple[int, int, int, int, int]]:
    """relpath -> (dev, ino, size, mode, mtime_ns) for every entry under root.

    mtime and mode are in on purpose: a "dry run" that wrote anything at all
    would touch something here, so this comparison is what makes "writes
    nothing" checkable rather than asserted.
    """
    out: dict[str, tuple[int, int, int, int, int]] = {}
    for cur, subdirs, names in os.walk(root):
        subdirs.sort()
        names.sort()
        for name in subdirs + names:
            target = Path(cur) / name
            st = os.lstat(target)
            out[str(target.relative_to(root))] = (
                st.st_dev,
                st.st_ino,
                st.st_size,
                stat.S_IMODE(st.st_mode),
                st.st_mtime_ns,
            )
    return out


def _offline_env(tmp_path: Path) -> dict[str, str]:
    """An environment whose `kubectl` records that it was called, then fails."""
    bindir = tmp_path / "poison-bin"
    bindir.mkdir(exist_ok=True)
    poison = bindir / "kubectl"
    poison.write_text(
        "#!/usr/bin/env bash\n"
        'printf "kubectl %s\\n" "$*" >> "$POISON_LOG"\n'
        "exit 99\n",
        encoding="utf-8",
    )
    poison.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["POISON_LOG"] = str(tmp_path / "kubectl-was-called.log")
    env.pop("KUBECONFIG", None)
    return env


def _run(args: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(REPO),
    )


@pytest.fixture()
def export_root(tmp_path: Path) -> Path:
    """A pre-N27 export, shaped like the live one (2026-09-26).

    Mirrors the live listing: the six platform namespaces, `_runtime` (a
    `.checkpoints` with one image, two records), `.route-b` with a numeric
    slot, an *empty* `.uid_reservations`, a stale `.uid_pool.lock`, and a
    couple of `sbx_*` trees. The live trees carry no top-level `sandbox.json`
    any more, which is why the script classifies by name and not by that
    record.
    """
    root = tmp_path / "export"
    root.mkdir()
    for name in STAY_AT_EXPORT_ROOT:
        (root / name).mkdir()
    (root / "_volumes" / "vol_1").mkdir()
    (root / "_volumes" / "vol_1" / "data.bin").write_bytes(b"volume-data")
    image = root / "_runtime" / ".checkpoints" / "sbx_aaa"
    image.mkdir(parents=True)
    (image / "latest").write_bytes(b"image-bytes")
    for sandbox_id in ("sbx_aaa", "sbx_bbb"):
        record = root / "_runtime" / sandbox_id
        record.mkdir()
        (record / "sandbox.json").write_text(
            json.dumps({"sandbox_id": sandbox_id}) + "\n", encoding="utf-8"
        )
        (record / "command-logs.jsonl").write_text('{"cmd": "echo"}\n', encoding="utf-8")
    (root / ".route-b" / "10000").mkdir(parents=True)
    (root / ".uid_reservations").mkdir()
    (root / ".uid_pool.lock").write_text("", encoding="utf-8")
    (root / ".uid_pool.lock").chmod(0o600)
    (root / "sbx_aaa" / "workspace").mkdir(parents=True)
    (root / "sbx_aaa" / "policy.json").write_text("{}\n", encoding="utf-8")
    (root / "sbx_aaa" / "workspace" / "hello.txt").write_text("hi\n", encoding="utf-8")
    (root / "sbx_bbb").mkdir()
    (root / "sbx_bbb" / "blob.bin").write_bytes(b"\x00" * 32)
    return root


# --- static contract: the script ------------------------------------------


def test_the_script_defaults_to_dry_run() -> None:
    lines = _lines(SCRIPT)
    # The default, and the only unconditional assignment; `--apply` is the one
    # arm that may flip it, and it does so in the argument loop.
    assert [line for line in lines if line.startswith("DRY_RUN=")] == ["DRY_RUN=1"]
    assert "--apply) DRY_RUN=0 ;;" in lines


def test_the_script_never_removes_anything_recursively() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    # The one delete in the script is `--delete-after`, and the engine does it
    # with `os.unlink` (one file) and `os.rmdir` (an empty directory). `rmdir`
    # is the guard that matters: it refuses a non-empty directory, so no tree,
    # no checkpoint image and no record can be removed by that flag. No
    # `shutil` means no `rmtree` anywhere.
    assert "rm -rf" not in text
    assert "shutil" not in text
    engine = _embedded("PY_ENGINE", SCRIPT)
    assert "os.unlink(stale)" in engine
    assert "os.rmdir(path)" in engine


def test_the_script_requires_the_worker_to_be_scaled_to_zero() -> None:
    lines = _lines(SCRIPT)
    assert "want_replicas=0" in lines
    assert (
        'replicas="$(kubectl -n "$NAMESPACE" get statefulset/e2b-worker '
        "-o jsonpath='{.spec.replicas}')\""
    ) in lines
    # Scale-down is not enough on its own: a terminating pod still holds the
    # volume. The gate reads the pods too, and says how to fix it.
    assert 'pods="$(kubectl -n "$NAMESPACE" get pods -l app=e2b-worker -o name)"' in lines
    assert "scale statefulset/e2b-worker --replicas=0" in SCRIPT.read_text(encoding="utf-8")


def test_the_kubeconfig_gate_names_the_projects_own_file() -> None:
    lines = _lines(SCRIPT)
    assert 'want="$REPO_ROOT/tmp/k0s/kubeconfig"' in lines
    assert "docs/deploy-clusters.md" in SCRIPT.read_text(encoding="utf-8")


def test_the_engine_heredoc_is_inside_a_function_not_a_command_substitution() -> None:
    """macOS `/bin/bash` is 3.2, and it mis-parses `x="$(cat <<'PY')"`.

    Scanning the `$( )` it follows the apostrophes *inside* the heredoc body --
    and a python body is made of them. The dev machine's default bash is that
    one, so the engine body lives in a function's heredoc and is piped out.
    """
    lines = _lines(SCRIPT)
    assert "py_engine() {" in lines
    assert [line for line in lines if "$(cat <<'PY_ENGINE'" in line] == []


def test_no_shell_variable_is_left_adjacent_to_a_non_ascii_character() -> None:
    """`$want）` parses as `want<first byte>: unbound variable` under bash 3.2.

    macOS `/bin/bash` is 3.2 and not multibyte-aware: it swallows the first
    byte of a full-width punctuation mark that follows a bare `$VAR` into the
    variable name, and `set -u` aborts the script on the spot. This file had
    four such sites (`$want）`, `$JOB（`, `$CONFIGMAP）`, `$CONFIGMAP（`) -- the
    same shape `migrate-state-owner.sh` was fixed for. A variable next to
    full-width punctuation must therefore be spelled `${VAR}`.
    """
    offenders = []
    for number, line in enumerate(_lines(SCRIPT), start=1):
        for match in re.finditer(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", line):
            offenders.append((number, match.group(0)))
    assert offenders == []


def test_usage_prints_exactly_the_leading_comment_block() -> None:
    """`usage()`'s `sed` range must be the comment block's real extent.

    The leading comment block is the script's own `--help`: it starts on line
    2 (line 1 is the shebang) and ends at the last comment line before the
    first statement. A range that overshoots spills a real command into the
    help text; one that falls short hides the tail of it.
    """
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    last_comment = 1  # the shebang is line 1; the block starts on line 2
    for number, line in enumerate(lines, start=1):
        if number < 2:
            continue
        if not line.startswith("#"):
            break
        last_comment = number
    assert (
        f"sed -n '2,{last_comment}p' \"$SCRIPT_PATH\""
        in SCRIPT.read_text(encoding="utf-8")
    ), f"usage() should read lines 2..{last_comment}"


def test_the_migration_stages_every_file_it_creates_at_0600() -> None:
    """The hard requirement: nothing this script creates is world-readable.

    `umask 077` covers every file the script (and the engine it pipes) creates;
    the journal is the one file that outlives the run, so its mode is set
    explicitly rather than left to the umask alone. Neither a `0644` nor a
    `0666` may appear anywhere.
    """
    lines = _lines(SCRIPT)
    text = SCRIPT.read_text(encoding="utf-8")
    assert "umask 077" in lines
    assert "os.chmod(journal_path, 0o600)" in _embedded("PY_ENGINE", SCRIPT)
    assert "0o644" not in text
    assert "0o666" not in text


def test_delete_after_is_a_separate_flag_with_no_confirmation_token() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "--delete-after" in text
    # Deliberately no "I know what I am doing" token in front of it: the flag's
    # own delete set (the stale lock and the empty reservation shell) is what
    # bounds it, not a prompt.
    assert "--i-know" not in text.split("--delete-after")[0][-200:]
    assert '"--delete-after"' in text


# --- static contract: the Job --------------------------------------------


def test_the_job_runs_as_root_on_the_shared_volume_without_retries() -> None:
    job = yaml.safe_load(JOB.read_text(encoding="utf-8"))
    assert job["apiVersion"] == "batch/v1"
    assert job["kind"] == "Job"
    assert job["metadata"]["namespace"] == "sandlock"
    # One migration: a half-done run must not be retried automatically.
    assert job["spec"]["backoffLimit"] == 0
    pod = job["spec"]["template"]["spec"]
    assert pod["restartPolicy"] == "Never"
    # NFS + rename(2): only uid 0 may re-own the trees, and only 0 may read the
    # 0600 journal back on a rollback run.
    assert pod["securityContext"]["runAsUser"] == 0
    (container,) = pod["containers"]
    assert container["image"] == (
        "registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:__IMAGE_VERSION__"
    )
    assert container["command"] == ["bash", "/scripts/migrate-state-base.sh"]
    # Rendered by the operator script. Applied straight from the file the
    # placeholder is an unknown argument and the script refuses -- so
    # `kubectl apply -f state-base-migrate.yaml` cannot write anything.
    assert container["args"] == ["__ENGINE_FLAGS__"]
    variables = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert variables == {
        "MIGRATE_ROOT": "/shared",
        "MIGRATE_WORKER_REPLICAS": "__WORKER_REPLICAS__",
    }
    mounts = {mount["mountPath"]: mount for mount in container["volumeMounts"]}
    assert mounts["/shared"] == {"name": "shared", "mountPath": "/shared"}
    assert mounts["/scripts"] == {
        "name": "script",
        "mountPath": "/scripts",
        "readOnly": True,
    }
    volumes = {volume["name"]: volume for volume in pod["volumes"]}
    assert volumes["shared"]["persistentVolumeClaim"] == {"claimName": "sandbox-shared"}
    assert volumes["script"]["configMap"] == {
        "name": "state-base-migrate",
        "defaultMode": 0o444,
    }


def test_the_job_is_not_part_of_the_rendered_overlay() -> None:
    """It is a one-shot an operator applies by hand, never an `apply.sh` member."""
    kustomization = yaml.safe_load(KUSTOMIZATION.read_text(encoding="utf-8"))
    # C1's broker DaemonSet is deliberately *not* here: it belongs to the
    # baseline (`deploy/k8s/priv-broker.yaml`, pulled in through `../k8s`), so
    # this overlay stays what it says it is -- the distribution differences
    # (seccomp root, NAS PV, NodePort) plus the capacity patch. This one-shot
    # Job is the other kind of non-member: it is rendered with the operator's
    # placeholders and applies by hand.
    assert kustomization["resources"] == [
        "../k8s",
        "storage-nas.yaml",
        "gateway-nodeport.yaml",
    ]


# --- behaviour, offline ---------------------------------------------------


def test_a_dry_run_plans_every_move_and_writes_nothing(
    export_root: Path, tmp_path: Path
) -> None:
    before = _inventory(export_root)
    proc = _run(["--root", str(export_root)], env=_offline_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert f"mode=plan root={export_root} dry_run=1" in lines
    runtime = _counts(export_root / "_runtime")
    route_b = _counts(export_root / ".route-b")
    tree_a = _counts(export_root / "sbx_aaa")
    tree_b = _counts(export_root / "sbx_bbb")
    # The order is the brief's: the two state directories first (the platform's
    # own files hang off them), then the trees.
    assert [line for line in lines if line.startswith("MOVE ")] == [
        f"MOVE _runtime -> state/_runtime kind=state files={runtime[0]} dirs={runtime[1]} bytes={runtime[2]}",
        f"MOVE .route-b -> state/.route-b kind=state files={route_b[0]} dirs={route_b[1]} bytes={route_b[2]}",
        f"MOVE sbx_aaa -> workspaces/sbx_aaa kind=tree files={tree_a[0]} dirs={tree_a[1]} bytes={tree_a[2]}",
        f"MOVE sbx_bbb -> workspaces/sbx_bbb kind=tree files={tree_b[0]} dirs={tree_b[1]} bytes={tree_b[2]}",
    ]
    assert [line for line in lines if line.startswith("MKDIR ")] == [
        "MKDIR state mode=1777",
        "MKDIR workspaces mode=1777",
        "MKDIR workspaces/_migrate mode=1777",
    ]
    assert "KEEP .uid_pool.lock kind=regular" in lines
    assert "LEAVE .uid_reservations kind=empty-dir（已确认空；只有 --delete-after 才删）" in lines
    assert [line for line in lines if line.startswith("STAY ")] == [
        f"STAY {name}" for name in STAY_AT_EXPORT_ROOT
    ]
    assert "SUMMARY mode=plan moves=4 dirs=3 todo=7 done=0 unknown=0" in lines
    # ...and nothing at all happened: same entries, inodes, modes, mtimes, and
    # no new file (which the poisoned kubectl also has a word on).
    assert _inventory(export_root) == before
    assert not (tmp_path / "kubectl-was-called.log").exists()


def test_apply_renames_every_tree_and_leaves_the_old_platform_alone(
    export_root: Path, tmp_path: Path
) -> None:
    before = _inventory(export_root)
    proc = _run(["--root", str(export_root), "--apply"], env=_offline_env(tmp_path))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert "SUMMARY mode=apply moves=4 dirs=3 todo=7 done=7 unknown=0" in lines

    after = _inventory(export_root)
    # rename(2), not copy: the very same inode is at the new path...
    for name in ("sbx_aaa", "sbx_bbb"):
        assert after[f"workspaces/{name}"] == before[name], name
        assert name not in after
    for name in ("_runtime", ".route-b"):
        assert after[f"state/{name}"] == before[name], name
        assert name not in after
    # ...the platform's own namespaces are untouched (inode *and* mtime)...
    for name in STAY_AT_EXPORT_ROOT:
        assert after[name] == before[name], name
    assert after["_volumes/vol_1/data.bin"] == before["_volumes/vol_1/data.bin"]
    # ...the checkpoint image came along inside `_runtime`, byte for byte...
    latest = export_root / STATE_DIR / "_runtime" / ".checkpoints" / "sbx_aaa" / "latest"
    assert latest.read_bytes() == b"image-bytes"
    # ...the stale lock stays (only `--delete-after` removes it) and so does the
    # reservation shell (it is confirmed empty, not moved).
    assert after[".uid_pool.lock"] == before[".uid_pool.lock"]
    assert after[".uid_reservations"] == before[".uid_reservations"]

    # The three directories this creates are the ones the worker's init would
    # style: 1777 for the roots the worker puts top-level names in, and the
    # checkpoint gate travels with `_runtime` unchanged.
    for rel in (STATE_DIR, TREES_DIR, f"{TREES_DIR}/_migrate"):
        assert stat.S_IMODE(os.lstat(export_root / rel).st_mode) == 0o1777, rel
    gate = export_root / STATE_DIR / "_runtime" / ".checkpoints"
    assert stat.S_IMODE(os.lstat(gate).st_mode) == 0o755

    journal_path = export_root / JOURNAL_REL
    assert stat.S_IMODE(os.lstat(journal_path).st_mode) == 0o600
    journal = journal_path.read_text(encoding="utf-8").splitlines()
    assert [line for line in journal if line.startswith("move\t")] == [
        "move\t_runtime\tstate/_runtime",
        "move\t.route-b\tstate/.route-b",
        "move\tsbx_aaa\tworkspaces/sbx_aaa",
        "move\tsbx_bbb\tworkspaces/sbx_bbb",
    ]
    assert [line for line in journal if line.startswith("mkdir\t")] == [
        "mkdir\tstate",
        "mkdir\tworkspaces",
        "mkdir\tworkspaces/_migrate",
    ]
    assert not (tmp_path / "kubectl-was-called.log").exists()


def test_apply_then_rollback_restores_the_original_shape(
    export_root: Path, tmp_path: Path
) -> None:
    before = _inventory(export_root)
    env = _offline_env(tmp_path)
    assert _run(["--root", str(export_root), "--apply"], env=env).returncode == 0
    proc = _run(["--root", str(export_root), "--apply", "--rollback"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        "SUMMARY mode=rollback moves=4 dirs=3 todo=7 done=7 unknown=0" in proc.stdout.splitlines()
    )
    after = _inventory(export_root)
    # Every original entry is back *at its original inode* (the reverse is a
    # rename too), and the three directories the migration created are gone.
    for rel, stamp in before.items():
        assert after[rel] == stamp, rel
    for rel in (STATE_DIR, TREES_DIR):
        assert rel not in after, rel
    # The journal is evidence, not state: it survives the rollback, moved out
    # of the directory that had to empty for the reverse to complete.
    rolled_back = export_root / ".state-base-migration.journal.rolled-back"
    assert stat.S_IMODE(os.lstat(rolled_back).st_mode) == 0o600
    assert "move\t_runtime\tstate/_runtime" in rolled_back.read_text(encoding="utf-8")


def test_a_rollback_dry_run_writes_nothing(export_root: Path, tmp_path: Path) -> None:
    env = _offline_env(tmp_path)
    assert _run(["--root", str(export_root), "--apply"], env=env).returncode == 0
    before = _inventory(export_root)
    proc = _run(["--root", str(export_root), "--rollback"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        "SUMMARY mode=rollback moves=4 dirs=3 todo=7 done=0 unknown=0"
        in proc.stdout.splitlines()
    )
    assert _inventory(export_root) == before


def test_a_create_in_flight_refuses_the_migration(export_root: Path, tmp_path: Path) -> None:
    (export_root / ".uid_reservations" / "sbx_new").write_text("10042\n", encoding="utf-8")
    proc = _run(["--root", str(export_root), "--apply"], env=_offline_env(tmp_path))
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == (
        "REFUSE(2): .uid_reservations 非空（有在途建箱，搬走会丢预约）：sbx_new"
    )
    assert not (export_root / STATE_DIR).exists()
    assert (export_root / ".uid_reservations" / "sbx_new").read_text(encoding="utf-8") == "10042\n"


def test_an_unrecognised_top_level_entry_refuses_the_migration(
    export_root: Path, tmp_path: Path
) -> None:
    (export_root / "README").write_text("not a tree\n", encoding="utf-8")
    proc = _run(["--root", str(export_root), "--apply"], env=_offline_env(tmp_path))
    assert proc.returncode == 3
    assert proc.stderr.splitlines()[0] == (
        "REFUSE(3): 顶层有本脚本不认识的条目（既不是目录、也不是 .uid_pool.lock）：README"
    )
    assert not (export_root / STATE_DIR).exists()


def test_an_already_migrated_export_refuses_rather_than_guessing(
    export_root: Path, tmp_path: Path
) -> None:
    env = _offline_env(tmp_path)
    assert _run(["--root", str(export_root), "--apply"], env=env).returncode == 0
    proc = _run(["--root", str(export_root), "--apply"], env=env)
    assert proc.returncode == 3
    assert proc.stderr.splitlines()[0].startswith(
        "REFUSE(3): 没有可搬的条目（旧位置都空、新位置都在）"
    )
    # ...and the second run really changed nothing.
    assert (export_root / TREES_DIR / "sbx_aaa" / "workspace" / "hello.txt").is_file()


def test_delete_after_removes_the_stale_lock_and_the_empty_shell(
    export_root: Path, tmp_path: Path
) -> None:
    env = _offline_env(tmp_path)
    proc = _run(["--root", str(export_root), "--apply", "--delete-after"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = proc.stdout.splitlines()
    assert "DELETE .uid_pool.lock" in lines
    assert "RMDIR .uid_reservations" in lines
    after = _inventory(export_root)
    assert ".uid_pool.lock" not in after
    assert ".uid_reservations" not in after
    # The payload -- the trees, the records and the checkpoint image -- is not
    # the flag's business, and neither is the platform's own storage.
    assert (export_root / TREES_DIR / "sbx_aaa" / "workspace" / "hello.txt").read_text(
        encoding="utf-8"
    ) == "hi\n"
    assert (export_root / STATE_DIR / "_runtime" / "sbx_aaa" / "sandbox.json").is_file()
    assert (export_root / STATE_DIR / "_runtime" / ".checkpoints" / "sbx_aaa" / "latest").is_file()
    assert (export_root / "_volumes" / "vol_1" / "data.bin").read_bytes() == b"volume-data"


# --- the operator path, with a stubbed kubectl ----------------------------


STUB_KUBECTL = '''#!/usr/bin/env python3
"""A kubectl that records its argv and answers the calls the driver makes."""
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(argv) + "\\n")
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
if argv[:2] == ["get", "nodes"]:
    # Cluster-scoped, so deliberately no `-n`: the driver has to be able to
    # check *which* cluster it is talking to before anything else.
    nodes = [
        {"metadata": {"name": "izuf697v12g31dyz4uvsjlz"},
         "status": {"nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}}},
        {"metadata": {"name": "izuf6d1usviqv6x9qk1hpcz"},
         "status": {"nodeInfo": {"architecture": "arm64", "kubeletVersion": "v1.36.4+k0s"}}},
    ]
    print(json.dumps({"items": nodes}))
    raise SystemExit(0)
assert argv[:2] == ["-n", "sandlock"], argv
rest = argv[2:]
if rest[:2] == ["get", "statefulset/e2b-worker"]:
    print(os.environ.get("STUB_REPLICAS", "0"))
elif rest[:2] == ["get", "pods"]:
    print("")
elif rest[:1] == ["exec"]:
    assert stdin.startswith("#!/usr/bin/env python3"), stdin[:60]
    print("SUMMARY mode=plan moves=4 dirs=3 todo=7 done=0 unknown=0")
elif rest[:2] == ["create", "configmap"]:
    print("apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: state-base-migrate\\n")
elif rest[:1] == ["apply"]:
    with open(os.environ["STUB_APPLIED"], "a", encoding="utf-8") as fh:
        fh.write(stdin)
elif rest[:3] == ["get", "job", "state-base-migrate"]:
    print("1" if argv[-1].endswith(".status.succeeded}") else "0")
elif rest[:1] == ["logs"]:
    print("JOB LOG LINE")
elif rest[:1] == ["delete"]:
    pass
else:
    raise SystemExit("unhandled argv: %r" % (argv,))
'''


def _stub_env(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    bindir = tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "kubectl"
    stub.write_text(STUB_KUBECTL, encoding="utf-8")
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["KUBECONFIG"] = str(REPO / "tmp" / "k0s" / "kubeconfig")
    env["STUB_LOG"] = str(tmp_path / "kubectl-argv.jsonl")
    env["STUB_APPLIED"] = str(tmp_path / "applied.yaml")
    return env, tmp_path / "kubectl-argv.jsonl", tmp_path / "applied.yaml"


def _recorded(log: Path) -> list[list[str]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _has_prefix(recorded: list[list[str]], *argv: str) -> bool:
    """`argv` as the leading elements of a recorded call (flags may follow)."""
    return any(entry[: len(argv)] == list(argv) for entry in recorded)


def test_the_operator_path_plans_through_the_control_plane(tmp_path: Path) -> None:
    env, log, applied = _stub_env(tmp_path)
    proc = _run([], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUMMARY mode=plan moves=4 dirs=3 todo=7 done=0 unknown=0" in proc.stdout.splitlines()
    recorded = _recorded(log)
    assert ["get", "nodes", "-o", "json"] in recorded
    assert [
        "-n",
        "sandlock",
        "get",
        "statefulset/e2b-worker",
        "-o",
        "jsonpath={.spec.replicas}",
    ] in recorded
    assert ["-n", "sandlock", "get", "pods", "-l", "app=e2b-worker", "-o", "name"] in recorded
    # The plan is read *inside* the cluster, from the pod that has the export
    # mounted read-only -- the workers are gone during the window, so the
    # control plane is the only reader that is left.
    assert [
        "-n",
        "sandlock",
        "exec",
        "-i",
        "deploy/control-plane",
        "-c",
        "control-plane",
        "--",
        "python3",
        "-",
        "--root",
        "/var/lib/e2b-sandboxes",
        "--mode",
        "plan",
    ] in recorded
    assert not applied.exists()


def test_the_operator_path_refuses_when_the_worker_is_still_up(tmp_path: Path) -> None:
    """The gate is what makes "worker 缩到 0" a precondition, not a step order."""
    env, _, applied = _stub_env(tmp_path)
    env["STUB_REPLICAS"] = "2"
    proc = _run(["--apply"], env=env)
    assert proc.returncode == 2
    # The identity self-check prints the node list it read to stderr before this
    # gate runs, so the refusal is matched as an exact line rather than as the
    # first one.
    assert (
        "REFUSE(2): statefulset/e2b-worker 有 2 个副本（要 want_replicas=0）——"
        "先停写：kubectl -n sandlock scale statefulset/e2b-worker --replicas=0 "
        "&& kubectl -n sandlock wait --for=delete pod -l app=e2b-worker --timeout=300s"
    ) in proc.stderr.splitlines()
    assert not applied.exists()


def test_the_operator_path_renders_and_runs_the_job(tmp_path: Path) -> None:
    env, log, applied = _stub_env(tmp_path)
    proc = _run(["--apply"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "JOB LOG LINE" in proc.stdout.splitlines()

    rendered = applied.read_text(encoding="utf-8")
    assert re.search(r"__[A-Z_]+__", rendered) is None
    job = yaml.safe_load(rendered)
    (container,) = job["spec"]["template"]["spec"]["containers"]
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    assert container["image"] == (
        "registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:" + version
    )
    assert container["args"] == ["--in-cluster", "--apply"]
    variables = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert variables["MIGRATE_WORKER_REPLICAS"] == "0"
    assert variables["MIGRATE_ROOT"] == "/shared"

    recorded = _recorded(log)
    # The plan is read before anything is created, the ConfigMap is applied
    # idempotently, and the objects are cleaned up after the logs were taken.
    # The ConfigMap's `create | apply` pair races with itself (both halves of a
    # pipeline), so only their *shared* position is asserted; the Job's own
    # `apply` is the last one, issued after that pipeline exited.
    exec_index = next(index for index, argv in enumerate(recorded) if argv[2:3] == ["exec"])
    create_index = next(index for index, argv in enumerate(recorded) if argv[2:4] == ["create", "configmap"])
    job_apply_index = max(
        index for index, argv in enumerate(recorded) if argv[2:3] == ["apply"]
    )
    logs_index = next(index for index, argv in enumerate(recorded) if argv[2:3] == ["logs"])
    delete_index = next(index for index, argv in enumerate(recorded) if argv[2:3] == ["delete"])
    assert exec_index < create_index < job_apply_index < logs_index < delete_index
    assert _has_prefix(recorded, "-n", "sandlock", "logs", "job/state-base-migrate")
    assert _has_prefix(recorded, "-n", "sandlock", "delete", "job", "state-base-migrate")
    assert _has_prefix(recorded, "-n", "sandlock", "delete", "configmap", "state-base-migrate")
    assert [
        "-n",
        "sandlock",
        "create",
        "configmap",
        "state-base-migrate",
        f"--from-file=migrate-state-base.sh={SCRIPT}",
        "--dry-run=client",
        "-o",
        "yaml",
    ] in recorded


def test_the_operator_path_rolls_back_through_the_same_job_manifest(tmp_path: Path) -> None:
    env, log, applied = _stub_env(tmp_path)
    proc = _run(["--rollback", "--apply"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (container,) = yaml.safe_load(applied.read_text(encoding="utf-8"))["spec"]["template"]["spec"][
        "containers"
    ]
    assert container["args"] == ["--in-cluster", "--apply", "--rollback"]
    assert _has_prefix(_recorded(log), "-n", "sandlock", "delete", "job", "state-base-migrate")


def test_the_offline_rehearsal_never_asks_the_cluster_anything(tmp_path: Path) -> None:
    """`--root` is a rehearsal: no gate needs a cluster, so none are faked."""
    env = _offline_env(tmp_path)
    root = tmp_path / "rehearsal"
    root.mkdir()
    for name in STAY_AT_EXPORT_ROOT:
        (root / name).mkdir()
    (root / "_runtime").mkdir()
    proc = _run(["--root", str(root)], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUMMARY mode=plan moves=1 dirs=3 todo=4 done=0 unknown=0" in proc.stdout.splitlines()
    assert not (tmp_path / "kubectl-was-called.log").exists()


def test_the_in_cluster_path_refuses_without_the_observed_replica_count(
    export_root: Path, tmp_path: Path
) -> None:
    """The Job trusts the driver for "the worker is stopped" -- and only that.

    The one number it cannot observe itself is the replica count, so it is
    passed in; without it (a hand-applied manifest) the in-cluster path refuses
    before touching anything.
    """
    env = _offline_env(tmp_path)
    env["MIGRATE_ROOT"] = str(export_root)
    proc = _run(["--in-cluster", "--apply"], env=env)
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0].startswith(
        "REFUSE(2): MIGRATE_WORKER_REPLICAS=<未设> 不是 0"
    )
    assert not (export_root / STATE_DIR).exists()


def test_the_in_cluster_path_migrates_the_root_it_was_given(
    export_root: Path, tmp_path: Path
) -> None:
    env = _offline_env(tmp_path)
    env["MIGRATE_ROOT"] = str(export_root)
    env["MIGRATE_WORKER_REPLICAS"] = "0"
    proc = _run(["--in-cluster", "--apply"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "SUMMARY mode=apply moves=4 dirs=3 todo=7 done=7 unknown=0" in proc.stdout.splitlines()
    assert (export_root / TREES_DIR / "sbx_aaa" / "workspace" / "hello.txt").is_file()
    assert not (tmp_path / "kubectl-was-called.log").exists()


def test_a_hand_applied_job_manifest_cannot_write_anything(tmp_path: Path) -> None:
    """`args: [__ENGINE_FLAGS__]` is the Job's own fail-closed default.

    Applying `deploy/k8s-k0s/state-base-migrate.yaml` without the operator
    script must not migrate anything: the placeholder is an unknown argument
    and the argument parser refuses it.
    """
    proc = _run(["__ENGINE_FLAGS__"], env=_offline_env(tmp_path))
    assert proc.returncode == 2
    assert proc.stderr.splitlines()[0] == "REFUSE(2): 未知参数：__ENGINE_FLAGS__（--help 看用法）"


def test_the_operator_path_carries_delete_after_into_the_job(tmp_path: Path) -> None:
    env, _, applied = _stub_env(tmp_path)
    proc = _run(["--apply", "--delete-after"], env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (container,) = yaml.safe_load(applied.read_text(encoding="utf-8"))["spec"]["template"]["spec"][
        "containers"
    ]
    assert container["args"] == ["--in-cluster", "--apply", "--delete-after"]
