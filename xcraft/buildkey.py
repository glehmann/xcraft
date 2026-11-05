"""Build keys: what the builds of our packages are made from, as docs/build-keys.md specifies, section by section.

Nothing else computes them: xcraft.buildid numbers the builds by them, and xcraft.cache caches them by them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xcraft.builds import load_builds, locked_packages
from xcraft.container import BUILDENV, PACKAGES
from xcraft.graph import build_deps, reachable
from xcraft.localrepo import REPO_FILE
from xcraft.lock import cached_lock
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from xcraft.builds import BuildId, Builds
    from xcraft.lock import Lock
    from xcraft.project import Project

FORMAT = 4
"""The version of how xcraft gives the builds what their keys cover, besides the scripts of the builds, like the build
trees, see rpmbuild.prepare_tree(): changed with it, to build them all again. 2: the build trees of the files of the
keys, from the same reads, with the same modes and times, the configuration of the image in an order of its own. 3:
the pinned upstream packages only, checked against their SHA-256, see xcraft.pinned. 4: the directories of the build
trees of the same modes, whatever the umask."""
SCRIPTS = ("build.sh", "offline.py", "record.py", "made.py", "rpmdeps.py")
"""The scripts of the builds, in xcraft/buildenv/: see The recipe."""
RECIPE = hashlib.sha256("".join([REPO_FILE, *((BUILDENV / s).read_text() for s in SCRIPTS)]).encode()).hexdigest()
"""What makes the builds, besides the image and the packages: a change to it rebuilds them all, but see _SAME_BUILDS."""
_SAME_BUILDS: dict[str, str] = {
    # the replacements of packages of the image with the pinned packages they need, which failed without them
    "7436941e7cc32e09edb1c1a1791e09a9af861bf1db5f3d226f7e4585ab53a3bf": (
        "4b928b99728b60533308a1f147188072c2495311781bd54ff6441657aa19b2a5"
    ),
    # SIGPIPE and SIGXFSZ restored: a write to a closed pipe kills rather than fails with EPIPE, and an error
    "b9b332f9df354013f4f94a02c6ad1e10f47e13a708a9e358ef80f188e015f54d": (
        "4b928b99728b60533308a1f147188072c2495311781bd54ff6441657aa19b2a5"
    ),
}
"""Recipes making the same packages as an earlier one, by their hash, with the earliest one: their builds keep its
keys."""


def recipe_key() -> str:
    """Return the hash of the scripts of the builds: the earliest one making the same packages."""
    return _SAME_BUILDS.get(RECIPE, RECIPE)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def directories(project: Project) -> dict[str, str]:
    """Return the hash of the directory of each package: see The package directory."""
    return {name: hash_dir(path, project.root) for name, path in project.package_dirs.items()}


def recipe(project: Project) -> str:
    """Return the hash of the recipe: see The recipe."""
    config = project.config
    return _sha256(
        json.dumps(
            [
                FORMAT,
                recipe_key(),
                (BUILDENV / "Containerfile").read_text(),
                config.base_image,
                sorted({*config.packages, *PACKAGES}),
                config.macros,
                config.release_suffixes,
                {name: extra.model_dump() for name, extra in config.extra_upstream.items()},
                # but where the source packages are published from
                {name: repo.model_dump(exclude={"source_baseurl"}) for name, repo in config.repos.items()},
            ],
            sort_keys=True,
        )
    )


@dataclass(frozen=True)
class Platform:
    """What the keys cover of an architecture of the project, from its specs and the locks: see The dependencies."""

    arch: str
    deps: dict[str, frozenset[str]]
    """The packages of ours each package is built against."""
    binaries: dict[str, tuple[str, ...]]
    """The binary packages of each package."""

    @classmethod
    def of(cls, project: Project, lock: Lock, builds: Builds) -> Platform:
        """Return what the keys cover of the architecture of `project`, with `lock`, its image, and `builds`."""
        packages = locked_packages(project.parsed, builds, project.arch)
        deps = build_deps(packages, lock.image.get(project.arch, []), lock.roots.of(project.arch))
        return cls(
            project.arch,
            {name: frozenset(needed) for name, needed in deps.items()},
            {name: tuple(b.name for b in package.binaries) for name, package in packages.items()},
        )


def lock_inputs(
    on: list[Platform], lock: Lock, builds: Builds, name: str, ignored: set[str] | None = None
) -> dict[str, list]:
    """Return what `lock` and `builds` have for `name` on the architectures `on`, but the versions and the SHA-256 of
    the binary packages of the upstream source packages `ignored`, those xcraft reuse ignores the updates of: see The
    lock.
    """

    def pinned(arch: str, names: list[str]) -> dict[str, list[str]]:
        pins = lock.binaries.get(arch, {})
        if missing := [n for n in names if n not in pins]:  # rather than keys of unknown packages
            msg = f"the lock has no pinned {', '.join(missing)} for {arch}: see xcraft lock"
            raise RuntimeError(msg)
        return {
            n: [p.arch, p.source] if p.source in (ignored or set()) else [p.evr, p.arch, p.sha256, p.source]
            for n in names
            for p in [pins[n]]
        }

    def generated(platform: Platform) -> dict[str, list[list[str]]]:
        deps, arch = platform.deps, platform.arch
        binaries = {b for dep in reachable(deps, deps.get(name, set())) for b in platform.binaries[dep]}
        return {b: [builds.requires.of(arch).get(b, []), builds.provides.of(arch).get(b, [])] for b in sorted(binaries)}

    return {
        platform.arch: [
            pinned(platform.arch, lock.image.get(platform.arch, [])),
            pinned(platform.arch, lock.roots.of(platform.arch).get(name, [])),
            builds.buildrequires.of(platform.arch).get(name, []),
            generated(platform),
        ]
        for platform in on
    }


class Inputs:
    """What the build keys of a project are computed from, read once, from one lock and one lock of the builds: nothing
    else is read after, from the project or the files, see The build key.
    """

    def __init__(
        self,
        project: Project,
        hashes: dict[str, str] | None = None,
        lock: Lock | None = None,
        builds: Builds | None = None,
    ) -> None:
        """Read them for `project`, with the `hashes` of its package directories, those of directories() unless given,
        and `lock` and `builds`, its own unless given, like those of another commit.
        """
        self.lock = (lock or cached_lock(project)).model_copy(deep=True)
        self.builds = (builds or load_builds(project)).model_copy(deep=True)
        # with the specs `project` parsed already, which the locks do not change
        self.platforms = [
            Platform.of(project if p.platform == project.platform else p, self.lock, self.builds)
            for p in project.all_platforms()
        ]
        # the packages of ours each package is built against on any of the architectures: see The dependencies
        self.deps: dict[str, frozenset[str]] = {
            name: frozenset().union(*(p.deps.get(name, frozenset()) for p in self.platforms))
            for name in sorted({name for p in self.platforms for name in p.deps})
        }
        self.recipe = recipe(project)
        self.hashes = dict(hashes or directories(project))

    def of(self, component: set[str], ids: dict[str, BuildId]) -> dict[str, list]:
        """Return the inputs of the packages of `component`, by name, with the build `ids` of their dependencies: see
        The build key, and Cycles.
        """
        return {
            name: [
                self.hashes[name],
                self.recipe,
                lock_inputs(self.platforms, self.lock, self.builds, name),
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
