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
(which pins the pool's two declarations): the value is *read* from the fleet
manifests by `_fleet_base_image()`, not written here a third time. Bumping the
fleet's digest pin without the stacks (or one stack without the fleet) fails
these tests instead of silently splitting the two.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit.test_autoscaler_local_backend_shape import _fleet_base_images

REPO = Path(__file__).resolve().parent.parent.parent

BASE_IMAGE_KEY = "E2B_BASE_IMAGE"

#: Every compose file whose *declared* base image must resolve to the fleet's,
#: mapped to how many declarations it carries. The count is pinned (the same
#: way the pool pin counts its two sites) so a further declaration cannot hide
#: behind "the known ones are green": prose in these files may name an old
#: literal on purpose, a declaration may not.
#:
#: `deploy/compose/docker-compose.prod.yml` and `.../docker-compose.autoscale.yml`
#: (the pool, pinned by its own test) spell it `E2B_BASE_IMAGE: ${E2B_BASE_IMAGE:-...}`,
#: so the default is what a `.env`-less checkout runs; the dev/test/multinode
#: files name the image outright. Either form is compared by the *value* it
#: falls back to -- the form is not what this pin is about.
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
    one, and the JSON `E2B_AS_WORKER_ENV` blob -- whose key sits inside a
    single-quoted value, never at the start of a line -- cannot match.
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
