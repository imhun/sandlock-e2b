"""The TLS recipe has to produce a pair the **65534** control plane can read.

Review item 2 turned a live regression into a pin. `control_plane` serves the
pair uvicorn is handed (`control_plane.config.uvicorn_ssl_kwargs`), and since C3
Task 5 it is uid 65534 in every lane; `deploy/scripts/gen-tls-cert.sh` used to
write the key `0600` (and a `umask 077` operator got a `0600` cert too), which a
65534 reader cannot open -- measured on the `deploy/stack` shape 2026-09-30:
`PermissionError: [Errno 13] Permission denied` out of `load_cert_chain`, and
the container restart-looped. The generator is the *local-verification* recipe,
so it now writes both files with mode `0644` (same effective mode the k8s lane
gets from a `kubectl create secret tls` Secret, whose in-pod default mode is
0644) -- and the assertion below is on the mode the script leaves behind, run
for real.
"""

from __future__ import annotations

import stat
import subprocess

import pytest
import yaml

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
GEN = REPO / "deploy" / "scripts" / "gen-tls-cert.sh"

#: The control plane's uid (C3 Task 5, D24): k8s `runAsUser` and the compose
#: `user:` on all three `control-plane` services.
CP_UID = 65534

COMPOSE_STACKS = (
    REPO / "deploy" / "compose" / "docker-compose.prod.yml",
    REPO / "deploy" / "compose" / "docker-compose.multinode.yml",
    REPO / "deploy" / "stack" / "docker-compose.prod.yml",
)


def test_the_generator_writes_a_pair_the_control_plane_uid_can_read(
    tmp_path: Path,
) -> None:
    """Run the recipe, then look at what it left: mode 0644 on **both** files."""
    out = tmp_path / "tls"
    result = subprocess.run(
        [str(GEN), str(out), "control-plane"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    cert, key = out / "tls.crt", out / "tls.key"
    for path in (cert, key):
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o644, (str(path), oct(mode))
    # A 0644 file is readable by uid 65534 whatever owned it (the recipe cannot
    # `chown 65534`: a non-root operator is the normal case), which is the
    # property the live CP needs -- and the reason the mode, not an owner, is
    # what this recipe fixes.
    assert key.stat().st_size > 0
    assert f"DNS:control-plane" in result.stdout
    assert f"wrote {cert} and {key}" in result.stdout


def test_the_recipe_never_writes_a_mode_the_control_plane_cannot_read() -> None:
    """...and the script text cannot go back to `chmod 600`/`umask`-dependent.

    The behavioural arm above would catch it, but this is the half that names the
    regression: the pair is generated for a reader that is *not* the file's
    owner, so anything narrower than 0644 (or an unstated mode, which is what a
    `umask 077` operator turns into 0600) is the bug.
    """
    text = GEN.read_text(encoding="utf-8")
    codes = [
        line.strip()
        for line in text.splitlines()
        if line.strip().startswith("chmod")
    ]
    assert codes == ['chmod 644 "$cert" "$key"']
    # An unstated mode is the same bug in the other direction: `umask 077` would
    # leave a 0600 pair. (The comment above the line *explains* that, so this
    # looks at statements, not at prose.)
    statements = [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not [line for line in statements if line.startswith("chmod 600")]
    assert not [line for line in statements if line.startswith("umask")]


def test_every_stack_hands_the_pair_to_a_container_that_runs_as_the_cp_uid() -> None:
    """The manifest half: the mount is read-only and the env names the files.

    The recipe and the stacks have to agree on one thing -- the CP reads
    `/tls/tls.crt` + `/tls/tls.key` as uid 65534 -- so the paths the comment
    tells the operator to export are the paths the services actually carry.

    Only the two *production* stacks ship the mount: the multinode acceptance
    shape publishes plain HTTP on 3100 and carries neither the mount nor the
    variables, which is asserted too (a multinode stack that grew TLS would need
    the workers to trust the CA first -- see §7.12's note on that).
    """
    #: Keyed by the path inside the repo (`deploy/compose` and `deploy/stack`
    #: both ship a `docker-compose.prod.yml`).
    with_tls = {
        "deploy/compose/docker-compose.prod.yml",
        "deploy/stack/docker-compose.prod.yml",
    }
    for path in COMPOSE_STACKS:
        compose = yaml.safe_load(path.read_text(encoding="utf-8"))
        control_plane = compose["services"]["control-plane"]
        assert control_plane["user"] == "65534:65534", path.name
        mounts = [str(mount) for mount in control_plane["volumes"]]
        env = control_plane["environment"]
        if path.relative_to(REPO).as_posix() in with_tls:
            assert any(mount.startswith("./tls:/tls:ro") for mount in mounts), mounts
            assert env["E2B_TLS_CERT"] == "${E2B_TLS_CERT:-}", path.name
            assert env["E2B_TLS_KEY"] == "${E2B_TLS_KEY:-}", path.name
        else:
            assert not [m for m in mounts if "/tls" in m], mounts
            assert "E2B_TLS_CERT" not in env, path.name
            assert "E2B_TLS_KEY" not in env, path.name
        # The paths the script's own usage comment tells the operator to export.
        assert "E2B_TLS_CERT=/tls/tls.crt" in GEN.read_text(encoding="utf-8")
        assert "E2B_TLS_KEY=/tls/tls.key" in GEN.read_text(encoding="utf-8")
