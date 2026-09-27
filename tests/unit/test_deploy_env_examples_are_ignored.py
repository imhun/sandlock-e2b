"""Every tracked `deploy/**/*.env.example` must have an ignored instance file.

The deployment docs tell operators to `cp deploy/<x>/.env.example deploy/<x>/.env`.
What lands there is the operator's values -- ports and image tags today, and the
first credential key somebody adds tomorrow. Three of the four instance files
were gitignored and `deploy/compose/.env` was not, so the asymmetry was one
`git add -A` away from committing an operator's local env; "it happens to be
untracked right now" is not a property of the repository.

The check runs the real `git check-ignore` (`--no-index`, so it answers for the
path whether or not the file exists on this machine) rather than matching the
patterns here: a re-implementation of gitignore semantics is exactly the kind of
thing that stays green while the rule it stands for is gone. The expected set is
pinned by name -- a new `.env.example` with no matching ignore rule fails the
set assertion, and ignoring the templates themselves fails the positive control.

Falsifiability: delete the `deploy/compose/.env` line from `.gitignore` and the
`deploy/compose/.env` case goes red (that was the state of the tree when this
test was written).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
EXAMPLE_SUFFIX = ".env.example"

#: Pinned by name: adding one is a deliberate act, and it has to come with an
#: ignore rule (the derived case below is what enforces that).
EXPECTED_EXAMPLES = (
    "deploy/compose/.env.example",
    "deploy/scripts/acr.env.example",
    "deploy/scripts/bastion.env.example",
    "deploy/stack/.env.example",
)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(REPO), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _tracked_env_examples() -> tuple[str, ...]:
    listed = _git("ls-files", "-z", "deploy")
    assert listed.returncode == 0, listed.stderr
    return tuple(
        sorted(
            path
            for path in listed.stdout.split("\0")
            if path.endswith(EXAMPLE_SUFFIX)
        )
    )


def _is_ignored(path: str) -> bool:
    """Ask git itself, for a path that may not exist on this machine."""
    return _git("check-ignore", "--no-index", "-q", path).returncode == 0


def test_the_tracked_env_examples_are_the_pinned_set() -> None:
    assert _tracked_env_examples() == EXPECTED_EXAMPLES


@pytest.mark.parametrize("example", EXPECTED_EXAMPLES)
def test_the_instance_of_each_env_example_is_gitignored(example: str) -> None:
    instance = example[: -len(".example")]
    assert instance.endswith(".env"), instance
    assert _is_ignored(instance), (
        f"{instance} is not ignored: `cp {example} {instance}` would leave an "
        "operator's env file for the next `git add -A` to pick up"
    )


@pytest.mark.parametrize("example", EXPECTED_EXAMPLES)
def test_the_env_examples_themselves_stay_tracked(example: str) -> None:
    """Positive control: the rule may not be satisfied by ignoring everything."""
    assert not _is_ignored(example), f"{example} is the template and must be tracked"
