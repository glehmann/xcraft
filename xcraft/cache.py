"""The cache of the builds, under keys covering everything that goes into them, and their releases.

The key of a build is its build key (see xcraft.buildid), with its release
suffix, %autorev: the number of its build ID, and its development build
suffix, if any.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from xcraft import buildkey
from xcraft.graph import components, reachable
from xcraft.localrepo import LocalRepo
from xcraft.project import INTERNAL

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from xcraft.buildid import BuildId
    from xcraft.project import Project


class BuildError(RuntimeError):
    """Raised when a package cannot be built."""


def build_keys(
    project: Project,
    autorev: dict[str, str] | None = None,
    hashes: dict[str, str] | None = None,
    ids: dict[str, BuildId] | None = None,
) -> dict[str, str]:
    """Return the cache key of each package: its build key, the same on all the architectures, then its release
    suffix, %autorev: the number of its build ID, ``+b<n>``, and its development build suffix of `autorev`, if any.

    See xcraft.buildid. A development build, or one built against development builds, has the hash of its build key
    and of the cache keys of the builds of ours it is built against rather than its build key, but a reused build. The
    `hashes` of the package directories are those of buildkey.directories(), computed now by default, and the build
    `ids` are those computed then, unless given. The lock must be prepared: see resolution.prepare().
    """
    from xcraft.buildid import base, compute  # noqa: PLC0415, which imports this module

    ids = ids if ids is not None else compute(project, base(project), hashes)
    deps, autorev = project.deps, autorev or {}
    keys: dict[str, str] = {}
    for component in components(deps):  # the dependencies first
        outside = sorted({dep for name in component for dep in deps[name]} - component)
        if not component & autorev.keys() and all(release_suffix(keys[d]) == f"+b{ids[d].number}" for d in outside):
            keys |= {name: f"{ids[name].key}+b{ids[name].number}" for name in component}
            continue
        builds = {n: ids[n].key for n in component}
        for name in component:
            key = buildkey.development_key(builds, [keys[d] for d in outside], name)
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
    """
    repo = LocalRepo(project, project.state / "repo")
    repo.reset()
    for name in keys if names is None else names:
        if cached_build(project, name, keys[name]).exists():
            repo.add(cached_build(project, name, keys[name]))
    return repo


def deps_repo(project: Project, name: str, keys: dict[str, str]) -> LocalRepo:
    """Return the repository the build of `name` runs against: the builds of its dependencies, with their `keys`,
    which its build root can have.
    """
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
