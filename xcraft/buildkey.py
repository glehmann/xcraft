"""Build keys: what the builds of our packages are made from, as docs/build-keys.md specifies, section by section.

Nothing else computes them: xcraft.buildid numbers the builds by them, and xcraft.cache caches them by them.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from xcraft.container import BUILDENV, PACKAGES
from xcraft.graph import reachable
from xcraft.localrepo import REPO_FILE
from xcraft.lock import load_lock
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from xcraft.buildid import BuildId
    from xcraft.lock import Lock
    from xcraft.project import Project

SCRIPTS = ("build.sh", "offline.py", "record.py", "made.py", "rpmdeps.py")
"""The scripts of the builds, in xcraft/buildenv/: see The recipe."""
RECIPE = hashlib.sha256("".join([REPO_FILE, *((BUILDENV / s).read_text() for s in SCRIPTS)]).encode()).hexdigest()
"""What makes the builds, besides the image and the packages: a change to it rebuilds them all, but see _SAME_BUILDS."""
_SAME_BUILDS = {
    # offline.py: an EOF when a process dies first
    "520edd959d64e226fcbb86a704bf7dbb75e57fa262ef70da5319d4e31f3ec1bc":
        "6e4c3d480ddebb5ab8113785a8089d733175e35d369b7c50c1cea01fb65140bc",
    # record.py: without the latest versions, which another script looks for
    "9696604d19c16fc9fdebc6431bacf28b50585465cfb3148b8e041c4ff8153778":
        "6e4c3d480ddebb5ab8113785a8089d733175e35d369b7c50c1cea01fb65140bc",
    # build.sh and record.py: the providers of the dynamic BuildRequires too
    "e4d86b87110eac1922b1127fa00c796d08b757136324c97223ea31f34236d20b":
        "6e4c3d480ddebb5ab8113785a8089d733175e35d369b7c50c1cea01fb65140bc",
    # build.sh, record.py and made.py: the dynamic BuildRequires before installing them, and the packages made
    "91b1c1f54f53be472170ccaf7f1001959e51a8206718056bb0020a4d383690c5":
        "6e4c3d480ddebb5ab8113785a8089d733175e35d369b7c50c1cea01fb65140bc",
    # made.py and record.py: entry points, and the dependencies of rpm headers in rpmdeps.py
    "cd1db99022f6382f7e7363906e76061130bd36e1aa129d164e8917f48cb9ea3a":
        "6e4c3d480ddebb5ab8113785a8089d733175e35d369b7c50c1cea01fb65140bc",
    # build.sh: the description of autorev, in a comment
    "01628fd02a2650def128b24cbada7d71b0524ba141bfe0842f67b726a066f698":
        "6e4c3d480ddebb5ab8113785a8089d733175e35d369b7c50c1cea01fb65140bc",
}  # fmt: skip
"""Recipes making the same packages as an earlier one, by their hash, with the earliest one: their builds keep its
keys."""


def recipe_key() -> str:
    """Return the hash of the scripts of the builds: the earliest one making the same packages."""
    return _SAME_BUILDS.get(RECIPE, RECIPE)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def directories(project: Project) -> dict[str, str]:
    """Return the hash of the directory of each package: see The package directory."""
    return {name: hash_dir(path) for name, path in project.package_dirs.items()}


def recipe(project: Project) -> str:
    """Return the hash of the recipe: see The recipe."""
    config = project.config
    return _sha256(
        json.dumps(
            [
                recipe_key(),
                (BUILDENV / "Containerfile").read_text(),
                config.base_image,
                sorted({*config.packages, *PACKAGES}),
                config.macros,
                config.release_suffixes,
                config.extra_upstream,
                {name: repo.model_dump() for name, repo in config.repos.items()},
            ],
            sort_keys=True,
        )
    )


def platforms(project: Project) -> list[Project]:
    """Return the project on all its architectures, which the keys cover: itself on its own, with what it computed
    already.
    """
    return [project if p.platform == project.platform else p for p in project.all_platforms()]


def dependencies(on: list[Project]) -> dict[str, set[str]]:
    """Return the packages of ours each package is built against, on any of the architectures `on`: see The
    dependencies.
    """
    deps: dict[str, set[str]] = {}
    for platform in on:
        for name, needed in platform.deps.items():
            deps.setdefault(name, set()).update(needed)
    return deps


def lock_inputs(on: list[Project], lock: Lock, name: str, ignored: set[str] | None = None) -> dict[str, list]:
    """Return what `lock` has for `name` on the architectures `on`, but the versions of the upstream source packages
    `ignored`, those xcraft reuse ignores the updates of: see The lock.
    """

    def versions(sources: list[str]) -> dict[str, str | None]:
        if missing := [s for s in sources if s not in lock.sources]:  # rather than keys of unknown versions
            msg = f"the lock has no version of {', '.join(missing)}: see xcraft lock"
            raise RuntimeError(msg)
        return {source: None if source in (ignored or set()) else lock.sources[source] for source in sources}

    def generated(platform: Project) -> dict[str, list[list[str]]]:
        deps, arch = platform.deps, platform.arch
        binaries = {b.name for dep in reachable(deps, deps.get(name, set())) for b in platform.packages[dep].binaries}
        return {b: [lock.requires.of(arch).get(b, []), lock.provides.of(arch).get(b, [])] for b in sorted(binaries)}

    return {
        platform.arch: [
            versions(lock.image.get(platform.arch, [])),
            versions(lock.roots.of(platform.arch).get(name, [])),
            lock.buildrequires.of(platform.arch).get(name, []),
            generated(platform),
        ]
        for platform in on
    }


class Inputs:
    """What the build keys of a project are computed from, once: see The build key."""

    def __init__(self, project: Project, hashes: dict[str, str] | None = None) -> None:
        """Compute them for `project`, with the `hashes` of its package directories, those of directories() unless
        given.
        """
        self.platforms = platforms(project)
        self.lock = load_lock(project)
        self.deps = dependencies(self.platforms)
        self.recipe = recipe(project)
        self.hashes = hashes or directories(project)

    def of(self, component: set[str], ids: dict[str, BuildId]) -> dict[str, list]:
        """Return the inputs of the packages of `component`, by name, with the build `ids` of their dependencies: see
        The build key, and Cycles.
        """
        return {
            name: [
                self.hashes[name],
                self.recipe,
                lock_inputs(self.platforms, self.lock, name),
                [[dep, ids[dep].number, ids[dep].key] for dep in sorted(self.deps[name] - component)],
            ]
            for name in sorted(component)
        }


def key(inputs: dict[str, list], name: str) -> str:
    """Return the build key of `name`, of the `inputs` of its component: see The build key."""
    return _sha256(f"{json.dumps(inputs, sort_keys=True)}\0{name}")


def development_key(component: dict[str, list[str]], outside: dict[str, str], name: str) -> str:
    """Return the hash of the cache key of a development build of `name`, or of one built against development builds,
    with the build key and the development suffix of each package of its `component`, by name, and the cache keys of
    the packages it is built against, out of it, `outside`, by name: see The cache key.
    """
    together = _sha256(json.dumps([[component[n] for n in sorted(component)], [outside[n] for n in sorted(outside)]]))
    return _sha256(f"{together}\0{name}")
