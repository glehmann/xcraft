"""Build IDs: the numbers of the builds of our packages, with the build keys they are built from.

A package's build ID is in ``.buildid/<package>``, one line: its number and build key. The build key is the same on
all the architectures: it covers what goes into the package on any of them, from the package directory, the recipe
and the lock, and the build IDs of the packages of ours it is built against, but not its own number, which it decides.
A package keeps the build ID of the main branch, where the current branch forks from it, as long as its key is the
same, and gets the next number otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel

from xcraft import buildkey, process
from xcraft.display import console
from xcraft.graph import components
from xcraft.lock import load_lock, lock_path, parse_lock

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xcraft.project import Project

DIR = ".buildid"
"""The directory of the build IDs, in the project."""


class BuildIdError(RuntimeError):
    """Raised when the build IDs would change, which --locked forbids, or give a number to two different builds."""


class BuildId(BaseModel):
    """The build ID of a package."""

    number: int
    key: str
    """The build key, the same on all the architectures."""
    accepts: str | None = None
    """The build key a reused build is accepted for, the current one: an equivalence, chosen with xcraft reuse."""

    @classmethod
    def parse(cls, text: str) -> BuildId:
        """Return the build ID of the content of its file."""
        match text.split():
            case [number, key]:
                return cls(number=int(number), key=key)
            case [number, key, "accepts", accepts]:
                return cls(number=int(number), key=key, accepts=accepts)
        msg = f"not a build ID: {text}"
        raise ValueError(msg)

    def text(self) -> str:
        """Return the content of its file."""
        return f"{self.number} {self.key}" + (f" accepts {self.accepts}" if self.accepts else "") + "\n"

    def matches(self, key: str) -> bool:
        """Return whether a package of build `key` has this build ID: built from it, or reusing it for it."""
        return key in (self.key, self.accepts)


def compute(
    project: Project,
    fork: dict[str, BuildId] | None,
    hashes: dict[str, str] | None = None,
    reuse: Callable[[str, dict[str, BuildId]], bool] | None = None,
    shown: dict[str, dict[str, list]] | None = None,
) -> dict[str, BuildId]:
    """Return the build ID of each package, from those of the main branch where the current one forks from it, `fork`,
    see base(), with the `hashes` of the package directories (see xcraft.cache.inputs()).

    The build keys are those of xcraft.buildkey, see docs/build-keys.md: their inputs are put in `shown`, by package,
    if given.

    A package with the key of the fork keeps its build ID, and so does one reusing a build for the key, there or in the
    working tree. Otherwise, a package `reuse` takes, with the build IDs of those before it, reuses the build of the
    fork, accepting the key; and the others get the next number of the fork's. The numbers the main branch gave since
    the fork do not count: giving one again conflicts on merge, in the file of the package. Without the main branch,
    the numbers are those of the working tree, 1 for new packages: there is nothing to number them against.
    """  # noqa: D205
    inputs, current = buildkey.Inputs(project, hashes), read(project)
    ids: dict[str, BuildId] = {}
    for component in components(inputs.deps):  # the dependencies first
        of = inputs.of(component, ids)
        for name in sorted(component):
            key = buildkey.key(of, name)
            if shown is not None:
                shown[name] = of
            known = (current if fork is None else fork).get(name)
            reused = [i for i in (current.get(name),) if i and i.accepts]
            if found := next((i for i in [*filter(None, [known]), *reused] if i.matches(key)), None):
                ids[name] = found
            elif known and reuse and reuse(name, ids):
                ids[name] = known.model_copy(update={"accepts": key})
            elif fork is None:
                ids[name] = BuildId(number=known.number if known else 1, key=key)
            else:
                ids[name] = BuildId(number=known.number + 1 if known else 1, key=key)
    return ids


def read(project: Project) -> dict[str, BuildId]:
    """Return the build IDs of the working tree, but those that cannot be read, like those of a merge conflict."""
    ids = {}
    for path in sorted((project.root / DIR).glob("*")):
        try:
            ids[path.name] = BuildId.parse(path.read_text())
        except ValueError:
            continue
    return ids


def _git(project: Project, *args: str) -> str | None:
    result = process.run(["git", "-C", str(project.root), *args], check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else None


_commits: dict[tuple[str, str], dict[str, BuildId]] = {}


def _at(project: Project, commit: str) -> dict[str, BuildId]:
    """Return the build IDs of `commit`, a commit id, read once."""
    if (str(project.root), commit) not in _commits:
        _commits[str(project.root), commit] = {
            path.rsplit("/", 1)[-1]: BuildId.parse(_git(project, "show", f"{commit}:./{path}") or "")
            for path in (_git(project, "ls-tree", "--name-only", commit, f"{DIR}/") or "").splitlines()
        }
    return _commits[str(project.root), commit]


def _main(project: Project) -> str | None:
    """Return the commit of the main branch: the remote's, as last fetched, or else the local one, if any."""
    branch = project.config.main_branch
    return next(
        filter(None, (_git(project, "rev-parse", "-q", "--verify", ref) for ref in (f"origin/{branch}", branch))), None
    )


def base(project: Project) -> dict[str, BuildId] | None:
    """Return the build IDs of the main branch where the current one forks from it, or None out of a git repository,
    or without the main branch.
    """  # noqa: D205
    return _at(project, fork) if (fork := _fork(project)) else None


def _fork(project: Project) -> str | None:
    """Return the commit of the main branch where the current branch forks from it, if any: with a merge in progress,
    where the merge forks from it.
    """  # noqa: D205
    merging = _git(project, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return (main := _main(project)) and _git(project, "merge-base", main, "HEAD", *filter(None, [merging]))


def proposed(fork: dict[str, BuildId] | None, ids: dict[str, BuildId]) -> set[str]:
    """Return the packages whose build ID of `ids` is not the one of the main branch where the current one forks from
    it, `fork`, see base(): those of development builds, none without the main branch. A reused build is the main
    branch's.
    """  # noqa: D205
    if fork is None:
        return set()
    return {name for name, i in ids.items() if not ((j := fork.get(name)) and (j.number, j.key) == (i.number, i.key))}


def on_main(project: Project) -> bool:
    """Return whether the current commit is on the main branch, whose build IDs are those of the final builds."""
    return bool(main := _main(project)) and _git(project, "merge-base", "--is-ancestor", "HEAD", main) is not None


def check_first_parent(project: Project, ids: dict[str, BuildId]) -> None:
    """On the main branch, check that the packages whose build key changed since the first parent of the current
    commit have greater numbers: one number is never given to two different builds, even by a merge conflict resolved
    by keeping one side.
    """  # noqa: D205
    if not on_main(project):
        return
    if not (parent := _git(project, "rev-parse", "-q", "--verify", "HEAD^")):
        console.print("[yellow]warning[/]: the build IDs are not checked against the previous commit, not fetched")
        return
    previous = _at(project, parent)
    if reused := sorted(
        name for name, i in ids.items() if (p := previous.get(name)) and p.key != i.key and i.number <= p.number
    ):
        msg = f"build numbers given again to other builds, since the previous commit: {', '.join(reused)}"
        raise BuildIdError(msg)


def update(project: Project, reuse: Callable[[str, dict[str, BuildId]], bool] | None = None) -> list[str]:
    """Write the build IDs of the packages, and return those changed, those `reuse` takes reusing their builds, see
    compute(). When the project is frozen, fail instead if they would change, or, on the main branch, if they reuse the
    numbers of the previous commit for other builds.
    """  # noqa: D205
    ids, current = compute(project, base(project), reuse=reuse), read(project)
    changed = sorted(name for name in ids.keys() | current.keys() if ids.get(name) != current.get(name))
    if project.frozen:
        if changed:
            msg = f"the build IDs would change, which --locked forbids: {', '.join(changed)}"
            raise BuildIdError(msg)
        check_first_parent(project, ids)
    directory = project.root / DIR
    directory.mkdir(exist_ok=True)
    for name in changed:
        if name in ids:
            (directory / name).write_text(ids[name].text())
        else:
            (directory / name).unlink()
    return changed


def reuse(project: Project, names: list[str], sources: list[str]) -> list[str]:
    """Reuse the builds of the main branch where the current one forks from it, despite changes, of the packages
    `names`, but those whose directories changed, and of those whose only changes are updates of the upstream source
    packages `sources`, and return those changed.

    The packages built against them keep their builds too, unless they changed otherwise.
    """  # noqa: D205
    if not (fork := _fork(project)):
        msg = f"reusing builds needs the main branch, {project.config.main_branch}, in a git repository"
        raise BuildIdError(msg)
    forked = _at(project, fork)
    forked_lock = parse_lock(_git(project, "show", f"{fork}:./{lock_path(project).name}") or "")
    lock, updated = load_lock(project), set(sources)
    platforms = buildkey.platforms(project)
    deps = buildkey.dependencies(platforms)

    def changed(path: Path) -> bool:
        """Return whether the files of `path` changed since the fork, untracked ones included."""
        relative = path.relative_to(project.root).as_posix()
        return _git(project, "diff", "--quiet", fork, "--", relative) is None or bool(
            _git(project, "ls-files", "--others", "--exclude-standard", "--", relative)
        )

    def reusable(name: str, ids: dict[str, BuildId]) -> bool:
        if changed(project.package_dirs[name]):
            return False
        if name in names:
            return True
        return (
            bool(updated)
            and not changed(project.root / "xcraft.toml")
            and all((ids[d].number, ids[d].key) == (forked[d].number, forked[d].key)
                    for d in deps[name] if d in ids and d in forked)
            and buildkey.lock_inputs(platforms, lock, name, updated)
            == buildkey.lock_inputs(platforms, forked_lock, name, updated)
        )  # fmt: skip

    return update(project, reusable)
