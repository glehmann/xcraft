"""The cache of the builds, under keys covering everything that goes into them.

That is the package directory (spec, patches, ``sources`` manifest), the keys
of the packages it is built against, the build environment image and
platform, and the extra upstream RPMs.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

from xcraft.container import BUILDENV, image_tag
from xcraft.graph import components, reachable
from xcraft.localrepo import REPO_FILE, LocalRepo
from xcraft.resolution import resolve
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
    """Return the cache key of each package: a SHA-256 of all that makes its build.

    That is its package directory, the build environment image and recipe,
    the versions of the upstream packages of its build root, resolved, besides
    the image's ones, which the image id covers, the `autorev` of the test
    builds, and the keys of its dependencies. Packages depending on each other
    get the key of them all. The `hashes` of the package directories are those
    of inputs(), computed now by default. The lock must be prepared: see
    resolution.prepare().
    """
    resolved, deps = resolve(project), project.deps
    hashes = hashes or inputs(project)
    image = set(resolved.image)
    base = json.dumps([image_tag(project), project.platform, project.config.extra_upstream, recipe_key()])

    def package(name: str) -> str:
        buildroot = [
            f"{upstream}={resolved.packages[upstream].evr}"
            for upstream in resolved.buildroots.get(name, [])
            if upstream not in image
        ]
        return json.dumps([name, hashes[name], (autorev or {}).get(name), buildroot])

    keys: dict[str, str] = {}
    for component in components(deps):  # the dependencies first
        h = hashlib.sha256(base.encode())
        for name in sorted(component):
            h.update(package(name).encode())
        for dep in sorted({dep for name in component for dep in deps[name]} - component):
            h.update(keys[dep].encode())
        keys |= {name: hashlib.sha256(f"{h.hexdigest()}\0{name}".encode()).hexdigest() for name in component}
    return keys


def builds_dir(project: Project, name: str) -> Path:
    """Return where the builds of `name` are cached, one per key."""
    return project.state / "cache/builds" / project.arch / name


def cached_build(project: Project, name: str, key: str) -> Path:
    """Return where the build of `name` with `key` is cached."""
    return builds_dir(project, name) / key


def has_normal_build(project: Project, name: str) -> bool:
    """Return whether `name` has a cached build, of any version, besides the test builds."""
    return any(
        not (build.name.startswith(".") or (build / "autorev").exists())  # not a build in progress, or a test one
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
    return f"builds/{project.arch}/{name}/{key}"


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
