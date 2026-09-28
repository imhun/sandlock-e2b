"""The C2 P0 probe: its guards and its verdict algebra, without needing a NAS.

The probe itself must run as root on the real shared store (that is the whole
point, and ``docs/c2-ownership-frontload.md`` §7 says so). What can be pinned
off-cluster is the part a wrong answer would silently corrupt:

* the guards -- no root, no verdict; ``--require-fstype`` never records an answer
  it could not prove came from the NAS;
* the verdict algebra -- which cells decide ``unusable`` / ``regression`` /
  ``zero-regression``, and that a cell the harness could not even build is
  reported as "unknown", never as "the storage said no";
* the fixtures -- in particular that the "created as X" fixture is really built
  by X, and that the shape which needs a hand-over says so;
* the runner's rendered Job -- placeholders gone, the real command line intact.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROBE = REPO_ROOT / "deploy" / "scripts" / "acceptance" / "probe_c2_ownership_p0.py"
RUNNER = REPO_ROOT / "deploy" / "scripts" / "acceptance" / "c2-p0-probe.sh"


def _load_probe():
    spec = importlib.util.spec_from_file_location("probe_c2_ownership_p0", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # `@dataclass` resolves the module through `sys.modules` while building the
    # class, so the module has to be registered before it is executed.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PROBE_MODULE = _load_probe()


#: What the probe **measured on this NAS** (2026-09-28, both arms identical), cell
#: by cell -- the record ``docs/c2-ownership-frontload.md`` §7 keeps. The surprise
#: is deliberate in the data: uid 0 *can* override another uid's tree here (the
#: server grants root), which is why the 2026-09-17 note it used to be checked
#: against is reported as ``no-longer``. Tests override individual cells to ask
#: "what would the verdict be then?".
EXPECTED_NAS_MATRIX = {
    "A1-fixture-ownership": "OK",
    "A2-as-X-rmtree-own-tree": "OK",
    "A3-as-X-create-in-1777": "OK",
    "A4-as-X-create-and-remove-in-1777": "OK",
    "A5-as-X-write-in-own-0770": "OK",
    "A6-as-X-chgrp-to-worker-gid": "ERR:EPERM",
    "B1-broker-listdir-0770": "OK",
    "B2-broker-stat-0660": "OK",
    "B3-broker-unlink-0660": "OK",
    "B4-broker-chown-verb": "OK",
    "B5-worker-group-bits": "OK",
    "C1-uid0-read-0600": "OK",
    "C2-uid0-listdir-0700": "OK",
    "C3-uid0-unlink-inside-0700": "OK",
    "C4-uid0-unlink-inside-0755": "OK",
    "C5-uid0-unlink-inside-0770": "OK",
    "C6-broker-unlink-inside-0770": "OK",
    "D1-other-unlink-in-1777": "ERR:EPERM",
    "D2-worker-unlink-in-1777": "ERR:EPERM",
    "D3-worker-unlink-in-0770": "OK",
    "D4-worker-rmtree-0700": "ERR:EACCES",
    # Re-measurement of the 2026-09-17 record (a 0600 file *created by 65534*).
    # Filled in from the cluster run; see docs/c2-ownership-frontload.md §7.
    "E1-uid0-read-worker-0600": "OK",
    "E2-broker-read-worker-0600": "OK",
    "E3-uid0-listdir-worker-0700": "OK",
    # The setgid candidate: a one-time `3777` + `umask 007` carries the worker's
    # group down the whole tree, keeps the sticky bit, and lets the worker delete
    # inside it -- all measured on the NAS (docs/c2-ownership-frontload.md §4.2).
    "F1-as-X-child-inherits-worker-group": "OK",
    "F1b-child-keeps-the-setgid-bit": "OK",
    "F2-as-X-grandchild-still-worker-group": "OK",
    "F3-worker-deletes-inside-inherited-tree": "OK",
    "F4-worker-removes-X-subtree": "OK",
}


def _rows(**overrides: str) -> dict[str, str]:
    """The expected NAS matrix, then the overrides. The matrix is data."""
    rows = {name: "OK" for name, _, _ in PROBE_MODULE.CHECKS}
    rows.update(EXPECTED_NAS_MATRIX)
    rows.update(overrides)
    return rows


def test_the_expected_matrix_covers_every_cell() -> None:
    assert sorted(EXPECTED_NAS_MATRIX) == sorted(
        name for name, _, _ in PROBE_MODULE.CHECKS
    )


# --- guards -----------------------------------------------------------------


def test_the_probe_refuses_to_run_without_root() -> None:
    assert PROBE_MODULE.root_refusal(0) is None
    assert PROBE_MODULE.root_refusal(1000) == (
        "must run as root (uid 0): every cell forks a child that setgid/setuid's "
        "into the subject identity, and this process is the only one that may. "
        "Run it through deploy/scripts/acceptance/c2-p0-probe.sh."
    )


def test_the_root_guard_refuses_a_missing_directory_and_slash() -> None:
    assert PROBE_MODULE.root_path_refusal(REPO_ROOT) is None
    assert PROBE_MODULE.root_path_refusal(Path("/")) == "--root must not be /"
    assert PROBE_MODULE.root_path_refusal(Path("/nonexistent-c2-p0-root")) == (
        "--root /nonexistent-c2-p0-root does not exist or is not a directory"
    )


def test_require_fstype_records_only_a_proven_nfs_answer() -> None:
    assert PROBE_MODULE.fstype_refusal("nfs4", "nfs") is None
    assert PROBE_MODULE.fstype_refusal("overlay", None) is None
    assert PROBE_MODULE.fstype_refusal("overlay", "nfs") == (
        "--require-fstype nfs: the scratch dir is on 'overlay'. The whole point of "
        "this probe is the NAS answer -- a local filesystem says yes to everything "
        "and hides the failures that matter."
    )
    assert PROBE_MODULE.fstype_refusal(None, "nfs") == (
        "--require-fstype nfs: could not determine the filesystem of the scratch dir "
        "(/proc/self/mountinfo unreadable) -- not recording a verdict."
    )


MOUNTINFO = "\n".join((
    "36 1 0:32 / / rw,relatime - overlay overlay rw",
    "420 36 0:45 / /var/lib/e2b-sandboxes rw - nfs4 172.18.0.1:/export rw,vers=4.0",
    "421 36 0:46 / /mnt/with\\040space rw - nfs4 172.18.0.1:/other rw",
    "422 36 0:47 / /var/lib/e2b-sandboxes/state rw - tmpfs tmpfs rw",
))


def test_mountinfo_picks_the_most_specific_mount() -> None:
    assert PROBE_MODULE.mount_from_mountinfo(
        MOUNTINFO, "/var/lib/e2b-sandboxes/_probes/c2-p0-x"
    ) == ("nfs4", "/var/lib/e2b-sandboxes")
    assert PROBE_MODULE.mount_from_mountinfo(
        MOUNTINFO, "/var/lib/e2b-sandboxes/state/_runtime"
    ) == ("tmpfs", "/var/lib/e2b-sandboxes/state")
    # Anything outside those mounts is covered by the root mount.
    assert PROBE_MODULE.mount_from_mountinfo(MOUNTINFO, "/elsewhere") == ("overlay", "/")
    assert PROBE_MODULE.mount_from_mountinfo("", "/elsewhere") == (None, None)
    # Mountinfo escapes a space in a mountpoint as \040.
    assert PROBE_MODULE.mount_from_mountinfo(
        MOUNTINFO, "/mnt/with space/y"
    ) == ("nfs4", "/mnt/with space")


def test_the_scratch_tree_is_named_by_time_and_pid() -> None:
    scratch = PROBE_MODULE.scratch_path(Path("/export"), now=0)
    assert scratch == Path("/export/_probes") / f"c2-p0-19700101T000000Z-{os.getpid()}"


# --- the verdict algebra ----------------------------------------------------


def test_the_measured_nas_matrix_is_zero_regression() -> None:
    verdict = PROBE_MODULE.verdicts(_rows())
    assert verdict["C1-CONTROL"] == "ok"
    assert verdict["C2-PREMISE"] == "ok"
    assert verdict["P0A-uid0-override"] == "yes"
    assert verdict["P0A-uid0-needs-the-group"] == "no"
    assert verdict["P0A-uid0-record-check"] == "no-longer"
    assert verdict["P0B-sticky"] == "enforced"
    assert verdict["P0B-x-can-chgrp"] == "no"
    assert verdict["P0C-setgid-inheritance"] == "yes"
    assert verdict["P0C-worker-deletes-via-group"] == "yes"
    assert verdict["P0C-sticky-preserved"] == "yes"
    assert verdict["C2-P0-VERDICT"] == "zero-regression"


def test_setgid_inheritance_is_partial_when_the_bit_is_dropped() -> None:
    """The group can come out right one level deep and still not travel further."""
    verdict = PROBE_MODULE.verdicts(_rows(**{
        "F1b-child-keeps-the-setgid-bit": "ERR:setgid-cleared:mode=0o770",
        "F2-as-X-grandchild-still-worker-group": "ERR:gid=10000",
    }))
    assert verdict["P0C-setgid-inheritance"] == "partial"
    assert verdict["C2-P0-VERDICT"] == "zero-regression"


def test_setgid_inheritance_is_no_when_the_group_never_lands() -> None:
    verdict = PROBE_MODULE.verdicts(_rows(**{
        "F1-as-X-child-inherits-worker-group": "ERR:gid=10000",
    }))
    assert verdict["P0C-setgid-inheritance"] == "no"


def test_a_storage_that_denies_uid0_override_matches_the_old_record() -> None:
    """The 2026-09-17 shape: uid 0 is refused, so the worker's group is load-bearing."""
    verdict = PROBE_MODULE.verdicts(_rows(**{
        "C1-uid0-read-0600": "ERR:EACCES",
        "C2-uid0-listdir-0700": "ERR:EACCES",
        "C3-uid0-unlink-inside-0700": "ERR:EACCES",
        "C5-uid0-unlink-inside-0770": "ERR:EACCES",
        "E1-uid0-read-worker-0600": "ERR:EACCES",
        "E3-uid0-listdir-worker-0700": "ERR:EACCES",
    }))
    assert verdict["P0A-uid0-override"] == "no"
    assert verdict["P0A-uid0-needs-the-group"] == "yes"
    assert verdict["P0A-uid0-record-check"] == "matches-record"
    assert verdict["C2-P0-VERDICT"] == "zero-regression"


def test_a_broken_control_makes_the_whole_measurement_unusable() -> None:
    verdict = PROBE_MODULE.verdicts(_rows(**{"B3-broker-unlink-0660": "ERR:EPERM"}))
    assert verdict["C1-CONTROL"] == "broken:B3-broker-unlink-0660=ERR:EPERM"
    assert verdict["C2-P0-VERDICT"] == "unusable:B3-broker-unlink-0660=ERR:EPERM"


def test_a_broken_premise_is_a_regression() -> None:
    verdict = PROBE_MODULE.verdicts(
        _rows(**{"A2-as-X-rmtree-own-tree": "ERR:EACCES"})
    )
    assert verdict["C2-PREMISE"] == "broken:A2-as-X-rmtree-own-tree=ERR:EACCES"
    assert verdict["C2-P0-VERDICT"] == "regression:A2-as-X-rmtree-own-tree=ERR:EACCES"


def test_a_cell_the_harness_could_not_build_is_unknown_not_a_denial() -> None:
    verdict = PROBE_MODULE.verdicts(
        _rows(**{"A1-fixture-ownership": "FIXTURE:dir chown 10000:65534 -> ERR:EPERM"})
    )
    assert verdict["C2-P0-VERDICT"] == (
        "unusable:A1-fixture-ownership=FIXTURE:dir chown 10000:65534 -> ERR:EPERM"
    )
    assert verdict["P0A-uid0-override"] == "unknown"
    assert verdict["P0B-sticky"] == "unknown"
    assert verdict["C2-PREMISE"] == "unknown"
    assert PROBE_MODULE.verdicts(_rows(**{"C6-broker-unlink-inside-0770": "CRASH:RuntimeError"}))[
        "C2-P0-VERDICT"
    ] == "unusable:C6-broker-unlink-inside-0770=CRASH:RuntimeError"


def test_the_p0a_and_p0b_facts_are_reported_separately_from_the_verdict() -> None:
    """A storage where X cannot chgrp is a *decision point*, not a C2 failure."""
    verdict = PROBE_MODULE.verdicts(_rows(**{
        "C1-uid0-read-0600": "ERR:EACCES",
        "C5-uid0-unlink-inside-0770": "ERR:EACCES",
        "A6-as-X-chgrp-to-worker-gid": "ERR:EPERM",
    }))
    assert verdict["P0A-uid0-override"] == "no"
    assert verdict["P0A-uid0-needs-the-group"] == "yes"
    assert verdict["P0B-x-can-chgrp"] == "no"
    assert verdict["C2-P0-VERDICT"] == "zero-regression"


# --- the fixtures -----------------------------------------------------------


def test_the_create_as_x_fixture_is_built_by_x_and_the_hand_over_one_by_root() -> None:
    specs = PROBE_MODULE.fixtures(worker_uid=65534, worker_gid=65534,
                                  pool_uid=10000, pool_gid=10000)
    created = specs["owner-0700"]
    assert (created.dir_builder, created.dir_uid, created.dir_gid) == ("pool", 10000, 10000)
    assert (created.file_builder, created.file_uid, created.file_gid) == ("pool", 10000, 10000)
    handed = specs["group-0770"]
    assert (handed.dir_builder, handed.dir_uid, handed.dir_gid) == ("root", 10000, 65534)
    assert (handed.dir_mode, handed.file_mode) == (0o770, 0o660)
    sticky = specs["sticky-1777"]
    assert (sticky.dir_builder, sticky.dir_mode) == ("root", 0o1777)
    assert (sticky.file_builder, sticky.file_uid) == ("pool", 10000)
    recorded = specs["worker-0700"]
    assert (recorded.dir_builder, recorded.dir_uid, recorded.dir_gid) == (
        "worker", 65534, 65534
    )
    assert (recorded.dir_mode, recorded.file_mode) == (0o700, 0o600)
    setgid = specs["setgid-parent"]
    assert (setgid.dir_builder, setgid.dir_uid, setgid.dir_gid) == ("root", 0, 65534)
    assert setgid.dir_mode == 0o3777  # setgid + sticky + rwx


# --- the runner and the Job -------------------------------------------------


def test_the_rendered_job_has_no_placeholders_and_the_real_command_line() -> None:
    rendered = subprocess.run(
        ["bash", str(RUNNER), "--render-job"],
        capture_output=True, text=True, check=True, cwd=str(REPO_ROOT),
    ).stdout
    version = (REPO_ROOT / "deploy" / "stack" / ".version").read_text().strip()
    assert "__IMAGE_VERSION__" not in rendered
    assert "__PROBE_ARGS__" not in rendered
    assert f"image: registry.cn-shanghai.aliyuncs.com/byteplan/e2b-sandlock-worker:{version}" in rendered
    assert (
        'args: ["--root", "/var/lib/e2b-sandboxes", "--require-fstype", "nfs", '
        '"--json", "--pool-uid", "10000", "--pool-gid", "10000"]'
    ) in rendered
    assert "claimName: sandbox-shared" in rendered
    # The default arm runs with the container's caps (root keeps DAC_OVERRIDE).
    assert "securityContext: {}\n" in rendered


def test_the_second_arm_drops_dac_override() -> None:
    """P0-a has two arms: uid 0 *with* and *without* the client-side override."""
    rendered = subprocess.run(
        ["bash", str(RUNNER), "--render-job", "--drop-dac-override"],
        capture_output=True, text=True, check=True, cwd=str(REPO_ROOT),
    ).stdout
    assert "securityContext: {capabilities: {drop: [DAC_OVERRIDE]}}\n" in rendered
    assert "securityContext: {}\n" not in rendered
    assert "__CAPS__" not in rendered
