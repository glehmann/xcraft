"""The cache of the builds, under keys covering everything that goes into them, and their releases.

The key of a build is its build key (see xcraft.buildid), with its release
suffix, %autorev: the number of its build ID, and its development build
suffix, if any.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from xcraft.container import BUILDENV
from xcraft.graph import components, reachable
from xcraft.localrepo import REPO_FILE, LocalRepo
from xcraft.project import INTERNAL
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from xcraft.project import Project

RECIPE = hashlib.sha256(
    "".join(
        [
            REPO_FILE,
            *((BUILDENV / s).read_text() for s in ("build.sh", "offline.py", "record.py", "made.py", "rpmdeps.py")),
        ]
    ).encode()
).hexdigest()
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
}  # fmt: skip
"""Recipes making the same packages as an earlier one, by their hash, with the earliest one: their builds keep its
keys."""


def recipe_key() -> str:
    """Return the recipe in the build keys: the earliest one making the same packages."""
    return _SAME_BUILDS.get(RECIPE, RECIPE)


class BuildError(RuntimeError):
    """Raised when a package cannot be built."""


def inputs(project: Project) -> dict[str, str]:
    """Return the hash of the directory of each package, its input in the keys."""
    return {name: hash_dir(path) for name, path in project.package_dirs.items()}


def build_keys(
    project: Project, autorev: dict[str, str] | None = None, hashes: dict[str, str] | None = None
) -> dict[str, str]:
    """Return the cache key of each package: its build key, the same on all the architectures, then its release
    suffix, %autorev: the number of its build ID, ``+b<n>``, and its development build suffix of `autorev`, if any.

    See xcraft.buildid. A development build, or one built against development builds, has the hash of its build key
    and of the cache keys of the builds of ours it is built against rather than its build key, but a reused build. The
    `hashes` of the package directories are those of inputs(), computed now by default. The lock must be prepared: see
    resolution.prepare().
    """  # noqa: D205
    from xcraft.buildid import base, compute  # noqa: PLC0415, which imports this module

    ids, deps, autorev = compute(project, base(project), hashes), project.deps, autorev or {}
    keys: dict[str, str] = {}
    for component in components(deps):  # the dependencies first
        outside = sorted({dep for name in component for dep in deps[name]} - component)
        if not component & autorev.keys() and all(release_suffix(keys[d]) == f"+b{ids[d].number}" for d in outside):
            keys |= {name: f"{ids[name].key}+b{ids[name].number}" for name in component}
            continue
        h = hashlib.sha256(json.dumps([[ids[n].key for n in sorted(component)], [keys[d] for d in outside]]).encode())
        for name in component:
            key = hashlib.sha256(f"{h.hexdigest()}\0{name}".encode()).hexdigest()
            keys[name] = f"{key}+b{ids[name].number}{autorev.get(name, '')}"
        # but a reused build, the main branch's, built against its builds
        keys |= {name: f"{ids[name].key}+b{ids[name].number}" for name in component if ids[name].accepts}
    return keys


def release_suffix(key: str) -> str:
    """Return the release suffix of a build, %autorev, from its cache `key`: what follows the hash."""
    return key[64:]


def builds_dir(project: Project, name: str) -> Path:
    """Return where the builds of `name` are cached, one per key."""
    return project.state / "cache/builds" / project.arch / name


def cached_build(project: Project, name: str, key: str) -> Path:
    """Return where the build of `name` with `key` is cached."""
    return builds_dir(project, name) / key


def has_normal_build(project: Project, name: str) -> bool:
    """Return whether `name` has a cached build, of any version, besides the development builds."""
    return any(
        not (
            build.name.startswith(".") or (build / "autorev").exists()
        )  # not a build in progress, or a development one
        for build in builds_dir(project, name).glob("*")
    )


def builds_repo(project: Project, keys: dict[str, str], names: Iterable[str] | None = None) -> LocalRepo:
    """Return the repository the builds run against, with the cached builds matching `keys`, of `names` only, or of
    all the packages.
    """  # noqa: D205
    repo = LocalRepo(project, project.state / "repo")
    repo.reset()
    for name in keys if names is None else names:
        if cached_build(project, name, keys[name]).exists():
            repo.add(cached_build(project, name, keys[name]))
    return repo


def deps_repo(project: Project, name: str, keys: dict[str, str]) -> LocalRepo:
    """Return the repository the build of `name` runs against: the builds of its dependencies, with their `keys`,
    which its build root can have.
    """  # noqa: D205
    return builds_repo(project, keys, reachable(project.deps, project.deps[name]))


def build_path(project: Project, name: str, key: str) -> str:
    """Return the path of the tree of a build in the remote storage."""
    return f"{INTERNAL}/builds/{project.arch}/{name}/{key}"


BUILD_FILES = ["RPMS", "SRPMS", "build.log"]
"""What a build is made of, in the cache and in the remote storage."""


def previous_duration(project: Project, name: str) -> float | None:
    """Return how long the last cached build of `name` took, if any."""
    builds = sorted(builds_dir(project, name).glob("*/build.log"), key=lambda log: log.stat().st_mtime)
    for log in reversed(builds):
        # the build environment runs the build under `time`
        if times := re.findall(r"^real\s+(\d+)m([\d.]+)s$", log.read_text(errors="replace"), re.MULTILINE):
            minutes, seconds = times[-1]
            return int(minutes) * 60 + float(seconds)
    return None
