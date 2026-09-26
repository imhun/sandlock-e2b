"""O3 Task 5: the dev machine's plaintext credential files must be mode 600.

`deploy/scripts/acr.env` and `deploy/scripts/bastion.env` hold the ACR push
password and the bastion SSH passphrase. Both are gitignored -- but both were
mode 644 on the dev machine, i.e. readable by every other local user. "Out of
git" is not the same as "unreadable".

So `deploy/scripts/lib/helpers.sh` checks each existing local credential file
*before* the two `.` lines and **refuses** (exit 1, repair command on stderr)
instead of warning and carrying on. It never chmods the file itself: a copy
that arrived in a shared directory is something a human has to look at, and the
script may run as a different user. `ALLOW_LOOSE_CREDENTIAL_FILES=1` is the one
explicit bypass (CI credentials that were never written to disk).

The behaviour cases run the real `helpers.sh` out of a throwaway scripts
directory, which is what makes exact assertions possible: stderr is compared
byte for byte, the refusal is checked to have left the file at 644, and the
credential value is checked to be absent from both streams. The sandbox lives
under `<repo>/tmp/` because this repo's rule is that temporary files stay in
the project's `tmp/` (system temp dirs are outside the sandbox write
whitelist).
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
HELPERS = REPO / "deploy" / "scripts" / "lib" / "helpers.sh"
RUNBOOK = (REPO / "docs" / "k8s-deployment.md").read_text(encoding="utf-8")
LOCAL_CREDENTIAL_FILES = (
    REPO / "deploy" / "scripts" / "bastion.env",
    REPO / "deploy" / "scripts" / "acr.env",
)
BYPASS_VAR = "ALLOW_LOOSE_CREDENTIAL_FILES"

BASTION_BODY = "BASTION_HOST=sandbox-bastion.example\n"
ACR_BODY = "ACR_USERNAME=sandbox-user\n"


def _mode(path: Path) -> str:
    """Permission bits, octal, the way the guard's message prints them."""
    return format(stat.S_IMODE(path.stat().st_mode), "o")


def _write(path: Path, body: str, mode: int) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture()
def scripts() -> Path:
    """A throwaway copy of `deploy/scripts/` holding only `lib/helpers.sh`."""
    root = REPO / "tmp" / "env-file-guard" / uuid.uuid4().hex[:12]
    dir_ = root / "scripts"
    (dir_ / "lib").mkdir(parents=True)
    shutil.copy2(HELPERS, dir_ / "lib" / "helpers.sh")
    try:
        yield dir_
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _source(
    scripts_dir: Path,
    tail: str = 'printf "sourced\\n"',
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """Source the sandbox helpers with a clean bypass variable."""
    env = {key: value for key, value in os.environ.items() if key != BYPASS_VAR}
    env.update(env_overrides or {})
    return subprocess.run(
        ["bash", "-c", f'. "{scripts_dir}/lib/helpers.sh"; {tail}'],
        capture_output=True,
        text=True,
        env=env,
    )


# --- the guard's contract --------------------------------------------------


def test_helpers_refuses_a_world_readable_credential_file() -> None:
    # The brief's pin, kept narrow: the guard names 600 and refuses.
    text = HELPERS.read_text(encoding="utf-8")
    assert "600" in text
    assert "refus" in text.lower()


def test_the_guard_names_the_exact_line_and_runs_before_both_sources() -> None:
    text = HELPERS.read_text(encoding="utf-8")
    assert "refuse: %s is mode %s, not 600 -- run: chmod 600 %s" in text
    guard = text.index("_require_local_credential_files")
    assert guard < text.index('. "$SCRIPT_DIR/bastion.env"')
    assert guard < text.index('. "$SCRIPT_DIR/acr.env"')


# --- behaviour: refusal ----------------------------------------------------


@pytest.mark.parametrize("loose_name", ["bastion.env", "acr.env"])
def test_a_644_credential_file_is_refused_and_left_alone(
    scripts: Path, loose_name: str
) -> None:
    loose = _write(scripts / loose_name, BASTION_BODY if loose_name == "bastion.env" else ACR_BODY, 0o644)
    _write(scripts / ("acr.env" if loose_name == "bastion.env" else "bastion.env"), ACR_BODY, 0o600)

    result = _source(scripts)

    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        f"refuse: {loose} is mode 644, not 600 -- run: chmod 600 {loose}\n"
    )
    assert _mode(loose) == "644"  # no silent chmod


@pytest.mark.parametrize(
    "mode", [0o640, 0o660, 0o604, 0o700, 0o400], ids=["640", "660", "604", "700", "400"]
)
def test_any_mode_other_than_exactly_600_is_refused(scripts: Path, mode: int) -> None:
    loose = _write(scripts / "bastion.env", BASTION_BODY, mode)
    _write(scripts / "acr.env", ACR_BODY, 0o600)

    result = _source(scripts)

    assert result.returncode == 1
    assert result.stderr == (
        f"refuse: {loose} is mode {format(mode, 'o')}, not 600"
        f" -- run: chmod 600 {loose}\n"
    )
    assert _mode(loose) == format(mode, "o")


def test_the_refusal_never_prints_the_credential_value(scripts: Path) -> None:
    secret = "sandbox-passphrase-abc123"
    loose = _write(
        scripts / "bastion.env", f"{BASTION_BODY}SSH_PASSPHRASE={secret}\n", 0o644
    )
    _write(scripts / "acr.env", ACR_BODY, 0o600)

    result = _source(scripts)

    assert result.returncode == 1
    assert result.stderr == (
        f"refuse: {loose} is mode 644, not 600 -- run: chmod 600 {loose}\n"
    )
    assert secret not in result.stderr
    assert secret not in result.stdout


# --- behaviour: the paths that must still work -----------------------------


def test_600_files_are_sourced_normally(scripts: Path) -> None:
    _write(scripts / "bastion.env", BASTION_BODY, 0o600)
    _write(scripts / "acr.env", ACR_BODY, 0o600)

    result = _source(scripts, tail='printf "%s|%s\\n" "$BASTION_HOST" "$ACR_USERNAME"')

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "sandbox-bastion.example|sandbox-user\n"


def test_missing_credential_files_are_not_an_error(scripts: Path) -> None:
    # The CI shape: no local file at all, credentials injected via the env.
    result = _source(scripts)

    assert result.returncode == 0
    assert result.stderr == ""
    assert result.stdout == "sourced\n"


def test_the_bypass_is_explicit_and_still_loads_the_file(scripts: Path) -> None:
    loose = _write(scripts / "bastion.env", BASTION_BODY, 0o644)

    result = _source(
        scripts,
        tail='printf "%s\\n" "$BASTION_HOST"',
        env_overrides={BYPASS_VAR: "1"},
    )

    assert result.returncode == 0
    assert result.stderr == (
        f"warning: {loose} is mode 644, not 600"
        f" -- allowed by {BYPASS_VAR}=1\n"
    )
    assert result.stdout == "sandbox-bastion.example\n"


# --- the real repo files ---------------------------------------------------


def test_the_real_scripts_dir_still_loads_with_no_bypass() -> None:
    # The toolchain pin: build-and-push.sh / open-cluster-tunnel.sh source this
    # file, so the guard itself must never be the thing that blocks a deploy.
    result = _source(REPO / "deploy" / "scripts")

    assert result.returncode == 0
    assert result.stdout == "sourced\n"


def test_the_local_credential_files_are_600_wherever_they_exist() -> None:
    for path in LOCAL_CREDENTIAL_FILES:
        assert not path.exists() or _mode(path) == "600", (
            f"{path} is mode {_mode(path) if path.exists() else 'absent'};"
            " the guard refuses anything but 600"
        )


# --- the runbook table the operator actually reads -------------------------

TABLE_4_TITLE = "### 表 4：开发机上的明文凭据（ACR / SSH，不进 Secret）"

#: Which credential each row is about, exactly as the table's first cell reads.
EXPECTED_CREDENTIALS = (
    "ACR 推送凭据（`deploy/scripts/acr.env` 的 `ACR_USERNAME`/`ACR_PASSWORD`，600）",
    "SSH 私钥 / 口令（`deploy/scripts/bastion.env` 的 `SSH_KEY`/`SSH_PASSPHRASE`，600）",
)

#: The irreversible point of each row (cell 4), exact: this is the thing a
#: reader has to see before pressing the button.
EXPECTED_IRREVERSIBLE = (
    "**删除旧凭据**（旧值只剩在本机 `acr.env` 里；删了就只能再轮换一次）",
    "**移除旧公钥**（之后没更新的本机失去部署能力；要回去只能再轮换一次）",
)

#: Fragments each row must carry exactly once. A dropped step or a lost fact
#: takes one of these with it.
EXPECTED_FRAGMENTS = (
    (
        "① 在云上新建一份专用凭据",
        "② 更新 `deploy/scripts/acr.env`",
        "③ `./deploy/scripts/build-and-push.sh` 验证能 push",
        "④ 若这套部署开了私有拉取，再同步 `E2B_IMAGE_REGISTRY_USERNAME`",
        "⑤ 删掉旧凭据",
        "`build-and-push.sh` 第 41 行的 `docker login` 立刻失败",
        "k0s 拉 ACR 是**匿名**的（§F9 实测 token 流程 200），pod 不带 `imagePullSecrets`",
    ),
    (
        "① 把新公钥追加到**跳板机**的 `authorized_keys`",
        "第二跳\"跳板机 → 节点\"**不带 `-i`**",
        "② 更新 `bastion.env`",
        "③ 验收：`deploy/scripts/open-cluster-tunnel.sh`",
        "`DRY_RUN=1 deploy/k8s-k0s/apply.sh`",
        "④ 从跳板机的 `authorized_keys` 移除旧公钥",
        "集群内部（pod 之间、NodePort 入口、已在跑的沙箱）不受影响",
    ),
)


def _table_4_rows() -> list[list[str]]:
    """表 4 parsed into rows of cells, header and separator dropped."""
    section = RUNBOOK.split(TABLE_4_TITLE, 1)
    assert len(section) == 2, "runbook carries no 表 4"
    body = section[1].split("\n### ", 1)[0]
    lines = [line.strip() for line in body.splitlines() if line.startswith("|")]
    rows = [[cell.strip() for cell in line.strip("|").split("|")] for line in lines]
    assert rows[0] == ["凭据", "步骤", "影响面", "不可逆点"]
    return rows[2:]


def test_the_runbook_carries_the_acr_and_ssh_table() -> None:
    rows = _table_4_rows()
    assert len(rows) == 2
    assert tuple(row[0] for row in rows) == EXPECTED_CREDENTIALS
    assert tuple(row[3] for row in rows) == EXPECTED_IRREVERSIBLE
    for row, expected in zip(rows, EXPECTED_FRAGMENTS):
        for fragment in expected:
            assert row[1].count(fragment) + row[2].count(fragment) == 1, fragment


def test_table_4_sits_with_the_other_tables_and_says_how_to_reconcile() -> None:
    assert RUNBOOK.index("### 表 3：") < RUNBOOK.index(TABLE_4_TITLE)
    assert RUNBOOK.index(TABLE_4_TITLE) < RUNBOOK.index("### 4.5.1")
    # The guard's own text, the reconciliation that replaces the Secret
    # fingerprint for these two files, and the "no plaintext in argv" note.
    assert "`refuse: <path> is mode <mode>, not 600 -- run: chmod 600 <path>`" in RUNBOOK
    assert "**对账就是权限**" in RUNBOOK
    assert "`-rw-------`" in RUNBOOK
    assert "**明文不进日志/argv**" in RUNBOOK
    assert "**私钥本体不在守卫范围内**" in RUNBOOK
