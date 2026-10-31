"""Build IDs: the numbers of the builds of our packages, with the build keys they are built from.

A package's build ID is in ``.buildid/<package>``, one line: its number and build key. The build key is the same on
all the architectures: it covers what goes into the package on any of them, from the package directory, the recipe
and the lock, and the build IDs of the packages of ours it is built against, but not its own number, which it decides.
A package keeps the build ID of the main branch, where the current branch forks from it, as long as its key is the
same, and gets the next number otherwise.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from xcraft import buildkey, process
from xcraft.display import console
from xcraft.graph import components
from xcraft.lock import load_lock, lock_path, parse_lock
from xcraft.project import write_atomically

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

    number: int = Field(ge=1)
    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    """The build key, the same on all the architectures."""
    accepts: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
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

    def same(self, other: BuildId | None) -> bool:
        """Return whether `other` is the same build: of the same number and key."""
        return other is not None and (self.number, self.key) == (other.number, other.key)


def compute(
    project: Project,
    fork: dict[str, BuildId] | None,
    hashes: dict[str, str] | None = None,
    reuse: Callable[[str, dict[str, BuildId]], bool] | None = None,
    shown: dict[str, dict[str, list]] | None = None,
    inputs: buildkey.Inputs | None = None,
) -> dict[str, BuildId]:
    """Return the build ID of each package, from those of the main branch where the current one forks from it, `fork`,
    see base(), from the `inputs` of the keys, read with the `hashes` of the package directories unless given (see
    xcraft.buildkey.Inputs).

    The build keys are those of xcraft.buildkey, see docs/build-keys.md: their inputs are put in `shown`, by package,
    if given.

    A package with the key of the fork keeps its build ID, and so does one reusing a build for the key, there or in the
    working tree, while it is the fork's. Otherwise, a package `reuse` takes, with the build IDs of those before it,
    reuses the build of the fork, accepting the key; and the others get the next number of the fork's. The numbers the
    main branch gave since the fork do not count: giving one again conflicts on merge, in the file of the package.
    Without the main branch, the numbers are those of the working tree, 1 for new packages: there is nothing to number
    them against.
    """
    inputs, current = inputs or buildkey.Inputs(project, hashes), read(project)
    ids: dict[str, BuildId] = {}
    for component in components(inputs.deps):  # the dependencies first
        of = inputs.of(component, ids)
        for name in sorted(component):
            key = buildkey.key(of, name)
            if shown is not None:
                shown[name] = of
            known = (current if fork is None else fork).get(name)
            # still the fork's build
            reused = [i for i in (current.get(name),) if i and i.accepts and (fork is None or i.same(fork.get(name)))]
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
    """Return the build IDs of the working tree, but those of a merge conflict, which update() gives again."""
    return {
        path.name: _parse(text, path.relative_to(project.root).as_posix())
        for path in sorted((project.root / DIR).glob("*"))
        if "<<<<<<<" not in (text := path.read_text())
    }


def _parse(text: str, where: str) -> BuildId:
    try:
        return BuildId.parse(text)
    except ValueError as e:
        msg = f"{where} is not a build ID: {text.strip()}"
        raise BuildIdError(msg) from e


def _git(project: Project, *args: str) -> str | None:
    result = process.run(["git", "-C", str(project.root), *args], check=False, capture=True)
    return result.stdout.strip() if result.returncode == 0 else None


_commits: dict[tuple[str, str], dict[str, BuildId]] = {}


def _at(project: Project, commit: str) -> dict[str, BuildId]:
    """Return the build IDs of `commit`, a commit id, read once."""
    if (str(project.root), commit) not in _commits:
        _commits[str(project.root), commit] = {
            path.rsplit("/", 1)[-1]: _parse(_git(project, "show", f"{commit}:./{path}") or "", f"{commit}:{path}")
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
    """
    return _at(project, fork) if (fork := _fork(project)) else None


def _fork(project: Project) -> str | None:
    """Return the commit of the main branch where the current branch forks from it, if any: with a merge in progress,
    where the merge forks from it.
    """
    merging = _git(project, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    return (main := _main(project)) and _git(project, "merge-base", main, "HEAD", *filter(None, [merging]))


def proposed(fork: dict[str, BuildId] | None, ids: dict[str, BuildId]) -> set[str]:
    """Return the packages whose build ID of `ids` is not the one of the main branch where the current one forks from
    it, `fork`, see base(): those of development builds, none without the main branch. A reused build is the main
    branch's.
    """
    if fork is None:
        return set()
    return {name for name, i in ids.items() if not i.same(fork.get(name))}


def on_main(project: Project) -> bool:
    """Return whether the current commit is on the main branch, whose build IDs are those of the final builds."""
    return bool(main := _main(project)) and _git(project, "merge-base", "--is-ancestor", "HEAD", main) is not None


def check_first_parent(project: Project, ids: dict[str, BuildId]) -> None:
    """On the main branch, check that the packages whose build key changed since the first parent of the current
    commit have greater numbers: one number is never given to two different builds, even by a merge conflict resolved
    by keeping one side.
    """
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
    """
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
            write_atomically(directory / name, ids[name].text())
        else:
            (directory / name).unlink()
    return changed


def reuse(project: Project, names: list[str], sources: list[str]) -> list[str]:
    """Reuse the builds of the main branch where the current one forks from it, despite changes, of the packages
    `names`, but those whose directories changed, and of those whose only changes are updates of the upstream source
    packages `sources`, and return those changed.

    The packages built against them keep their builds too, unless they changed otherwise.
    """
    if not (fork := _fork(project)):
        msg = f"reusing builds needs the main branch, {project.config.main_branch}, in a git repository"
        raise BuildIdError(msg)
    forked = _at(project, fork)
    forked_lock = parse_lock(_git(project, "show", f"{fork}:./{lock_path(project).name}") or "")
    lock, updated = load_lock(project), set(sources)
    # the inputs of the keys with the lock of the fork, the rest as it is now
    forked_inputs = buildkey.Inputs(project, lock=forked_lock)
    component_of = {name: component for component in components(forked_inputs.deps) for name in component}

    def built_alike(name: str) -> bool:
        """Return whether `name` has the key of the fork with its lock: only the lock changed since, not the recipe,
        nor the build IDs of the packages it is built against.
        """
        component = component_of[name]
        if name not in forked or any(d not in forked for n in component for d in forked_inputs.deps[n] - component):
            return False
        return forked[name].matches(buildkey.key(forked_inputs.of(component, forked), name))  # or reuses its build

    def changed(path: Path) -> bool:
        """Return whether the files of `path` changed since the fork, untracked ones included, with those its links
        point to, like those of another package: what its hash covers, see specs.hash_dir().
        """
        links = [p.resolve() for p in path.rglob("*") if p.is_symlink()]
        paths = [p.relative_to(project.root).as_posix() for p in [path, *links]]
        return _git(project, "diff", "--quiet", fork, "--", *paths) is None or bool(
            _git(project, "ls-files", "--others", "--exclude-standard", "--", *paths)
        )

    def reusable(name: str, ids: dict[str, BuildId]) -> bool:
        if changed(project.package_dirs[name]):
            return False
        if name in names:
            return True
        return (
            bool(updated)
            and built_alike(name)
            # the packages it is built against reuse their builds too
            and all(ids[d].same(forked[d]) for d in forked_inputs.deps[name] if d in ids and d in forked)
            and buildkey.lock_inputs(forked_inputs.platforms, lock, name, updated)
            == buildkey.lock_inputs(forked_inputs.platforms, forked_lock, name, updated)
        )  # fmt: skip

    return update(project, reusable)
