"""Build IDs: the numbers of the builds of our packages, with the build keys they are built from.

A package's build ID is in ``.buildid/<package>``, one line: its number and build key. The build key is the same on
all the architectures: it covers what goes into the package on any of them, from the package directory, the recipe
and the lock, and the build IDs of the packages of ours it is built against, but not its own number, which it decides.
A package keeps the build ID of the main branch, where the current branch forks from it, or of its tip, as long as its
key is the same, and gets the next number of both otherwise.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel

from xcraft import process
from xcraft.cache import recipe_key
from xcraft.container import BUILDENV, PACKAGES
from xcraft.display import console
from xcraft.graph import components, reachable
from xcraft.lock import load_lock
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from xcraft.project import Project

DIR = ".buildid"
"""The directory of the build IDs, in the project."""

type Branch = tuple[dict[str, BuildId], dict[str, BuildId]]
"""The build IDs of the main branch where the current one forks from it, and of its tip."""


class BuildIdError(RuntimeError):
    """Raised when the build IDs would change, which --locked forbids, or give a number to two different builds."""


class BuildId(BaseModel):
    """The build ID of a package."""

    number: int
    key: str
    """The build key, the same on all the architectures."""

    @classmethod
    def parse(cls, text: str) -> BuildId:
        """Return the build ID of the content of its file."""
        number, key = text.split()
        return cls(number=int(number), key=key)

    def text(self) -> str:
        """Return the content of its file."""
        return f"{self.number} {self.key}\n"


def _recipe(project: Project) -> str:
    """Return a hash of what makes the builds of all the packages, on any architecture, but the upstream packages."""
    config = project.config
    return hashlib.sha256(
        json.dumps(
            [
                recipe_key(),
                (BUILDENV / "Containerfile").read_text(),
                config.base_image,
                sorted({*config.packages, *PACKAGES}),
                config.macros,
                config.release_suffixes,
                config.extra_upstream,
                {name: repo.model_dump() for name, repo in project.repos.items()},
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()


def compute(project: Project, branch: Branch | None, hashes: dict[str, str] | None = None) -> dict[str, BuildId]:
    """Return the build ID of each package, from those of the main `branch`, see base(), with the `hashes` of the
    package directories (see xcraft.cache.inputs()).

    The build key covers the package directory, the recipe, and, on all the architectures, in their locked versions,
    the upstream packages of the image and of the build root, as the lock records them, with what the build root is
    resolved from: the dynamic BuildRequires and the requirements and provides rpm generated for the binary packages
    of ours it may have; and the build IDs of the packages of ours it is built against. Packages depending on each
    other, in a cycle, get the key of them all: they are rebuilt together.

    A package with the key of the main branch where the current one forks from it, or of its tip, keeps its build ID,
    the fork's first: one unchanged by the current branch keeps it while the main branch changed it. Otherwise, it gets
    the next number of both, as a merge of the main branch, even in progress, must not reuse its numbers. Without the
    main branch, the numbers are those of the working tree, 1 for new packages: there is nothing to number them against.
    """  # noqa: D205
    hashes = hashes or {name: hash_dir(path) for name, path in project.package_dirs.items()}
    lock, deps, current = load_lock(project), project.deps, read(project)
    arches = [platform.arch for platform in project.all_platforms()]
    common = [
        _recipe(project),
        {arch: {source: lock.sources[source] for source in lock.image.get(arch, [])} for arch in arches},
    ]
    fork, main = branch or (current, {})
    ids: dict[str, BuildId] = {}
    for component in components(deps):  # the dependencies first

        def inputs(name: str, component: set[str] = component) -> list:
            # the binary packages of ours its build root may have
            binaries = sorted({b.name for dep in reachable(deps, deps[name]) for b in project.packages[dep].binaries})
            return [
                hashes[name],
                {
                    arch: [
                        {source: lock.sources[source] for source in lock.roots.of(arch).get(name, [])},
                        lock.buildrequires.of(arch).get(name, []),
                        {b: [lock.requires.of(arch).get(b, []), lock.provides.of(arch).get(b, [])] for b in binaries},
                    ]
                    for arch in arches
                },
                [[dep, ids[dep].number, ids[dep].key] for dep in sorted(deps[name] - component)],
            ]

        together = json.dumps([common, {name: inputs(name) for name in sorted(component)}], sort_keys=True)
        for name in sorted(component):
            key = hashlib.sha256(f"{together}\0{name}".encode()).hexdigest()
            known = [i for i in (fork.get(name), main.get(name)) if i]
            if found := next((i for i in known if i.key == key), None):
                ids[name] = found
            elif branch is None:
                ids[name] = BuildId(number=known[0].number if known else 1, key=key)
            else:
                ids[name] = BuildId(number=max((i.number for i in known), default=0) + 1, key=key)
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


def base(project: Project) -> Branch | None:
    """Return the build IDs of the main branch where the current one forks from it, and of its tip; or None out of a
    git repository, or without the main branch.
    """  # noqa: D205
    if (main := _main(project)) and (fork := _git(project, "merge-base", "HEAD", main)):
        return _at(project, fork), _at(project, main)
    return None


def proposed(project: Project, ids: dict[str, BuildId]) -> set[str]:
    """Return the packages whose build ID of `ids` is not the main branch's: those of development builds, none
    without the main branch.
    """  # noqa: D205
    if (branch := base(project)) is None:
        return set()
    return {name for name, i in ids.items() if i not in (branch[0].get(name), branch[1].get(name))}


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


def update(project: Project) -> list[str]:
    """Write the build IDs of the packages, and return those changed. When the project is frozen, fail instead if
    they would change, or, on the main branch, if they reuse the numbers of the previous commit for other builds.
    """  # noqa: D205
    ids, current = compute(project, base(project)), read(project)
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
