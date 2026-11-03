# SPDX-License-Identifier: AGPL-3.0-or-later
"""One contract for running git against a memory vault.

Versioned memory is the product's central promise, and until now three
modules kept it three different ways. `memory_store._git_commit` returned a
hash it never checked, `pm._git_commit` returned a bool wrapped in a bare
`except Exception`, and `onboarding._git_commit` returned nothing at all.
None of them looked at a returncode.

That is not three implementations of one idea; it is three different
promises to the caller, and two of them promised nothing. In production
every git command had been failing with exit 128 -- the container ran as
root against vault directories owned by uid 1000, and git refuses to touch
a repository whose owner it doesn't recognise. Six of seven vaults held
zero commits. `memory_write` kept answering with an empty string where a
snapshot id belonged, and `memory_history` returned `[]` because it had the
production failure mode written into it as a fallback.

So: every command is checked, a failure carries git's own words, and
`commit()` refuses to hand back a hash it cannot prove exists. The one
non-failure that looks like a failure -- "nothing to commit", exit 1 --
is recognised explicitly rather than swallowed along with everything else.

Identity lives here too. A container has no `~/.gitconfig`, so
`user.name`/`user.email` are unset and `git commit` dies with "Author
identity unknown". `memory_store` passed identity through the environment
and the other two didn't, which means fixing only the ownership would have
left `pm_*` and onboarding broken in a new and equally quiet way.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_IDENTITY = {
    "GIT_AUTHOR_NAME": "memaix",
    "GIT_AUTHOR_EMAIL": "memaix@localhost",
    "GIT_COMMITTER_NAME": "memaix",
    "GIT_COMMITTER_EMAIL": "memaix@localhost",
}

# git translates. `_is_empty_commit` reads git's own wording, and on a Swedish
# host git answers "inget att commita" -- so the check passed in CI, where the
# locale is C, and failed on the developer's laptop. Pinning the locale here
# rather than hoping for English is the difference between a rule and a
# coincidence; it cost one failing test to learn, which is cheaper than
# learning it from a vault that stopped recording.
_LOCALE = {"LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"}


class GitError(RuntimeError):
    """A git command failed. The message carries git's own stderr.

    Raised rather than returned because every previous variant of this code
    returned, and the returns were never read.
    """


def env() -> dict:
    """The process environment plus a committer identity git will accept."""
    merged = os.environ.copy()
    merged.update(_IDENTITY)
    merged.update(_LOCALE)
    return merged


def run(vault: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run one git command inside *vault*.

    `-C` rather than `cwd=` so the failure message names the repository even
    when the directory is missing entirely -- `cwd=` would raise
    FileNotFoundError from Popen and lose which vault it was about.
    """
    proc = subprocess.run(
        # core.fsmonitor=false: a repo-local config must never name a command
        # that git then runs on `status` (defence in depth; files_* also
        # refuse .git paths).
        ["git", "-c", "core.fsmonitor=false", "-C", str(vault), *args],
        env=env(),
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(_describe(vault, args, proc))
    return proc


def _describe(vault: Path, args: list[str], proc: subprocess.CompletedProcess) -> str:
    """git's first line of complaint, attributed to a command and a vault.

    Only the first line: "dubious ownership" is followed by four lines of
    advice about `safe.directory` that would bury the fact in any log.
    """
    said = (proc.stderr or proc.stdout or "").strip().splitlines()
    detail = said[0] if said else f"exit {proc.returncode}"
    return f"git {' '.join(args)} failed in {vault}: {detail}"


def is_repo(vault: Path) -> bool:
    """Whether *vault* is a git repository this process can actually use.

    Deliberately not `(vault / ".git").exists()`. That is the check the old
    code made, and it is exactly why production looked healthy: `git init`
    creates the directory as root before there is any repository to
    distrust, so `.git` was present in all seven vaults while every command
    against them died. Asking git is the only answer that means anything.
    """
    return run(vault, ["rev-parse", "--git-dir"], check=False).returncode == 0


def require_repo(vault: Path) -> None:
    """Raise unless git will operate on *vault*, quoting git's refusal.

    Order matters wherever both this and `has_commits` are asked. A vault
    git refuses and a vault with nothing in it both answer "no commits", and
    reading that as "no history" is precisely the bug: production returned
    [] for months. Establish that the repository answers at all, and only
    then let emptiness mean emptiness.
    """
    run(vault, ["rev-parse", "--git-dir"])


def has_commits(vault: Path) -> bool:
    """Whether HEAD resolves. False for a freshly `init`-ed repository."""
    return run(vault, ["rev-parse", "--verify", "HEAD"], check=False).returncode == 0


def head(vault: Path) -> str:
    """The hash at HEAD, or "" when the repository has no commits yet."""
    if not has_commits(vault):
        return ""
    return run(vault, ["log", "-1", "--format=%H"]).stdout.strip()


def _is_empty_commit(proc: subprocess.CompletedProcess) -> bool:
    """Distinguish "there was nothing to record" from "git could not run".

    Writing the same bytes twice is a legitimate no-op, not a fault, and it
    is the one non-zero exit that must not raise. Matching on git's wording
    is fragile in principle; in practice `git commit` has said "nothing to
    commit" since before any of this existed, and the alternative -- a
    `diff --cached --quiet` probe before every commit -- doubles the number
    of git invocations per write to guard against a phrase that has not
    changed in fifteen years.
    """
    said = (proc.stdout + proc.stderr).lower()
    return "nothing to commit" in said or "nothing added to commit" in said


def commit(vault: Path, paths: list[str], message: str, *, all_tracked: bool = False) -> str:
    """Stage *paths* and commit them. Returns the resulting commit hash.

    Raises GitError if git fails, or if a commit somehow leaves no hash
    behind. That last check is the invariant the docstring in
    `memory_store` claimed for months while returning "" -- the point of
    `memory_write` returning a snapshot id is that `memory_revert` can be
    handed it later, and an empty string is not a snapshot id.

    `all_tracked` exists for onboarding, which writes several files across
    the vault and committed them with `add -A`. Kept, but named, so that a
    reader can see which caller sweeps up unrelated working-tree changes.
    """
    if all_tracked:
        run(vault, ["add", "-A"])
    elif paths:
        run(vault, ["add", "--", *paths])

    proc = run(vault, ["commit", "-m", message], check=False)
    if proc.returncode != 0 and not _is_empty_commit(proc):
        raise GitError(_describe(vault, ["commit"], proc))

    hash_ = head(vault)
    if not hash_:
        # Reachable when the very first commit had nothing to stage: the
        # write produced no change, so there is no HEAD to point at. Silent
        # here would mean handing back "" again, which is the whole bug.
        raise GitError(f"commit in {vault} produced no hash")
    return hash_


def init(vault: Path) -> None:
    """Make *vault* a usable repository, or say why it isn't.

    Checked, unlike the `git init` it replaces. An init that fails leaves a
    directory that looks initialised and answers nothing.
    """
    run(vault, ["init"])
    if not is_repo(vault):
        raise GitError(f"git init left {vault} unusable")
