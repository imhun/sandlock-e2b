"""Every compose stack builds sandboxes from the fleet's MCP-capable base.

N44 (`docs/open-issues.md`): the pool's base-image drift (N40, `ccab370`) was
not the only copy. `/usr/bin/mcp-gateway` -- what a sandbox's `/mcp` route
execs -- is `COPY`ed into the **base image** a sandbox's rootfs is built from
(`deploy/docker/Dockerfile.mcp-base`; the worker image carries its own copy at
the same path), so a stack whose base image defaults to the plain
`python:3.14-slim` answers every `/mcp` route with a 503 -- "mcp gateway failed
to start ... can't open file '/usr/bin/mcp-gateway'" -- and that drift is
shape-independent.

Same approach as
`tests/unit/test_autoscaler_local_backend_shape.py::test_the_pool_base_image_is_the_fleets_mcp_capable_one`
(which pinned the pool's two declarations, until the pool was retired on
2026-09-30): the value is *read* from the fleet manifests by
`_fleet_base_image()`, not written here a third time. Bumping the fleet's
digest pin without the stacks (or one stack without the fleet) fails these
tests instead of silently splitting the two.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent

BASE_IMAGE_KEY = "E2B_BASE_IMAGE"

#: Every compose file whose *declared* base image must resolve to the fleet's,
#: mapped to how many declarations it carries. The count is pinned (the same
#: way the pool pin counts its two sites) so a further declaration cannot hide
#: behind "the known ones are green": prose in these files may name an old
#: literal on purpose, a declaration may not.
#:
#: `deploy/compose/docker-compose.prod.yml` spells it
#: `E2B_BASE_IMAGE: ${E2B_BASE_IMAGE:-...}`, so the default is what a
#: `.env`-less checkout runs; the dev/test/multinode files name the image
#: outright. Either form is compared by the *value* it falls back to -- the
#: form is not what this pin is about. (The retired autoscale stack was the
#: other `${...:-...}` form here; it is gone as of 2026-09-30.)
DEFAULTED_STACKS = {
    "deploy/compose/docker-compose.prod.yml": 2,
    "deploy/compose/docker-compose.multinode.yml": 4,
    "deploy/compose/docker-compose.yml": 1,
    "deploy/compose/docker-compose.test.yml": 1,
}

#: The fleet's own stack. It carries this key as a bare `${E2B_BASE_IMAGE}`
#: (no in-tree default): its value lives in the node-local, untracked
#: `deploy/stack/.env`. See the test below for why it stays that way.
FLEET_STACK = "deploy/stack/docker-compose.prod.yml"

#: The digest placeholder `deploy/stack/.env.example` ships and the operator
#: fills in from `build-and-push.sh` (E6.2). Compared by substitution, so the
#: template is still pinned to the fleet's MCP-capable repository *and* tag.
STACK_DIGEST_PLACEHOLDER = "__E2B_BASE_IMAGE_DIGEST__"


def _k8s_env(key: str, manifest: str) -> str:
    """One k8s manifest's env value by name.

    Moved here with the pool's retirement (2026-09-30): the fleet files that
    name the base image in-tree are the two k8s manifests -- the fleet compose
    stack carries a bare `${E2B_BASE_IMAGE}` whose value lives in the
    node-local, untracked `deploy/stack/.env` -- and the worker manifest's own
    comment says they have to keep in step. Reading both is what makes a
    one-sided fleet drift red rather than silently the "known" value.
    """
    lines = [line.strip() for line in manifest.splitlines()]
    marker = f"- name: {key}"
    hits = [index for index, line in enumerate(lines) if line == marker]
    assert len(hits) == 1, f"expected exactly one {key!r} env entry: {hits}"
    # The value is the entry's business: some carry a comment block above it
    # (the base image's digest pin explains itself there), so walk past those.
    cursor = hits[0] + 1
    while cursor < len(lines) and (lines[cursor] == "" or lines[cursor][0] == "#"):
        cursor += 1
    assert cursor < len(lines), f"no value line after {key!r}"
    value_line = lines[cursor]
    assert value_line.startswith("value: "), value_line
    return value_line[len("value: ") :].strip().strip('"')


def _fleet_base_images() -> dict[str, str]:
    """The fleet's MCP-capable base image, as each manifest spells it."""
    worker = (REPO / "deploy" / "k8s" / "worker.yaml").read_text(encoding="utf-8")
    control = (REPO / "deploy" / "k8s" / "control-plane.yaml").read_text(
        encoding="utf-8"
    )
    return {
        "deploy/k8s/worker.yaml": _k8s_env(BASE_IMAGE_KEY, worker),
        "deploy/k8s/control-plane.yaml": _k8s_env(BASE_IMAGE_KEY, control),
    }


def _fleet_base_image() -> str:
    """The single base image both fleet manifests spell out.

    Read, not copied: `_fleet_base_images()` parses `deploy/k8s/worker.yaml`
    and `deploy/k8s/control-plane.yaml` (the two in-tree files that name the
    image; the compose stack's value is untracked), and the worker manifest's
    own comment says they have to keep in step.
    """
    fleet = _fleet_base_images()
    assert len(set(fleet.values())) == 1, fleet
    return fleet["deploy/k8s/worker.yaml"]


def _declared_base_images(path: Path) -> list[str]:
    """Every `E2B_BASE_IMAGE:` declaration in one compose/YAML file, in order.

    Line-oriented on purpose: what this pin is about is a declaration
    (`E2B_BASE_IMAGE: <value>`), so a comment that mentions the key (these
    files explain themselves in prose) is skipped rather than mistaken for
    one. (The retired autoscale stack also carried the key inside its
    single-quoted `E2B_AS_WORKER_ENV` JSON, never at the start of a line --
    that form is gone with the pool, and the line-orientation is what kept it
    from matching back then.)
    """
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped.startswith(f"{BASE_IMAGE_KEY}:"):
            continue
        # maxsplit: the value itself carries a `:tag@sha256:...` colon.
        values.append(stripped.split(":", 1)[1].strip().strip('"').strip("'"))
    return values


def _interpolated_default(value: str) -> str | None:
    """The default of a `${E2B_BASE_IMAGE:-<default>}` interpolation, else None."""
    prefix = "${" + BASE_IMAGE_KEY + ":-"
    if value.startswith(prefix) and value.endswith("}"):
        return value[len(prefix) : -1]
    return None


def test_every_stack_defaults_to_the_fleets_mcp_capable_base_image() -> None:
    """N44: `/mcp` needs a base image that carries `/usr/bin/mcp-gateway`.

    Every declaration in `DEFAULTED_STACKS` is compared against the fleet's
    own digest-pinned image, which is where the value comes from -- so this is
    a comparison between two in-tree declarations, not a third copy of a
    literal. Two directions drift red: the fleet bumping its pin while a stack
    keeps the old one, and a stack moving off the fleet value on its own.

    The old value (`python:3.14-slim`) is a plain Docker Hub image, and a
    sandbox built from it cannot start the gateway: the `/mcp` route answers
    `503` with `can't open file '/usr/bin/mcp-gateway'`, whatever the
    sandbox's shape is.
    """
    fleet_base = _fleet_base_image()
    for relative, expected_count in DEFAULTED_STACKS.items():
        values = _declared_base_images(REPO / relative)
        assert len(values) == expected_count, (relative, values)
        for value in values:
            default = _interpolated_default(value)
            # A bare literal is its own default; an interpolation falls back
            # to the part after `:-`. Both are compared to the fleet value.
            assert (value if default is None else default) == fleet_base, (
                relative,
                value,
            )


def test_the_fleet_stack_keeps_the_base_image_as_a_bare_env_override() -> None:
    """`deploy/stack/` is the fleet itself: no in-tree default to correct.

    The two sites here are bare `${E2B_BASE_IMAGE}` interpolations, so they
    have no default that could be the plain `python:3.14-slim` -- and the
    fleet's value is already where it belongs: the node-local, untracked
    `deploy/stack/.env` (its tracked template `deploy/stack/.env.example`
    pins `python-mcp:3.14@sha256:__E2B_BASE_IMAGE_DIGEST__`, and `upgrade.sh`
    refuses a tag-only ref there, E6.2).

    Pinned so a later "the same bug is here too" edit has to argue with the
    reason: giving these sites an in-tree default would (a) add a *third*
    literal of the fleet image to keep in step, and (b) silently pin a stack
    to image-rootfs on a host whose `.env` deliberately leaves the key empty
    (the pure-sandlock shape).
    """
    assert _declared_base_images(REPO / FLEET_STACK) == [
        "${" + BASE_IMAGE_KEY + "}",
        "${" + BASE_IMAGE_KEY + "}",
    ]


def _example_env_base_image(path: Path) -> str:
    """The single ``E2B_BASE_IMAGE=`` value an example env file declares.

    `.env` files spell an assignment ``KEY=value`` (the compose files spell the
    key ``KEY:``), and the digest pin's prose sits on comment lines above it, so
    this is line-oriented the same way `_declared_base_images` is. The count is
    pinned: a second declaration in one file could otherwise hide behind the
    first.
    """
    prefix = BASE_IMAGE_KEY + "="
    values: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            values.append(stripped[len(prefix) :].strip().strip('"').strip("'"))
    assert len(values) == 1, (str(path), values)
    return values[0]


def test_the_compose_example_env_defaults_to_the_fleets_mcp_capable_base_image() -> None:
    """The documented ``cp`` must not re-pin the stacks to a base without MCP.

    `README.md` and the header of `docker-compose.prod.yml` both tell the
    operator to run
    ``cp deploy/compose/.env.example deploy/compose/.env``, and that `.env`
    *wins* over the compose files' own ``${E2B_BASE_IMAGE:-...}`` default. With
    the stale ``python:3.11-slim@sha256:d1e9ca7c...`` literal here, following
    the docs re-introduced N44: sandboxes built from that base answer every
    `/mcp` route with ``503 ... can't open file '/usr/bin/mcp-gateway'``.
    """
    assert (
        _example_env_base_image(REPO / "deploy/compose/.env.example")
        == _fleet_base_image()
    )


def test_the_stack_example_env_keeps_the_fleets_mcp_capable_image() -> None:
    """The fleet's own template: the fleet image, digest still to be filled in.

    `deploy/stack/.env.example` is the tracked template for the untracked,
    node-local `deploy/stack/.env` that the bare ``${E2B_BASE_IMAGE}`` above
    reads. It already names the fleet's MCP-capable `python-mcp:3.14`; only the
    digest is a placeholder (E6.2: the operator substitutes the ACR manifest
    digest after `build-and-push.sh`). Pinned so "the other example env is
    stale too" cannot quietly move this one back to a non-MCP base either.
    """
    repository = _fleet_base_image().partition("@sha256:")[0]
    assert (
        _example_env_base_image(REPO / "deploy/stack/.env.example")
        == repository + "@sha256:" + STACK_DIGEST_PLACEHOLDER
    )
