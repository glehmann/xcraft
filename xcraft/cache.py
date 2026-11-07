"""The cache of the builds, under keys covering everything that goes into them, and their releases.

The key of a build is its build key (see xcraft.buildid), with its release
suffix, %autorev: the number of its build ID, and its development build
suffix, if any.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
from typing import TYPE_CHECKING

from xcraft import buildkey
from xcraft.graph import components, reachable
from xcraft.localrepo import LocalRepo
from xcraft.project import INTERNAL
from xcraft.signing import KEY_FILE, dev_signer

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from xcraft.builds import BuildId
    from xcraft.project import Project
    from xcraft.signing import Signer


class BuildError(RuntimeError):
    """Raised when a package cannot be built."""


def build_keys(
    project: Project,
    autorev: dict[str, str] | None = None,
    hashes: dict[str, str] | None = None,
    ids: dict[str, BuildId] | None = None,
    inputs: buildkey.Inputs | None = None,
) -> dict[str, str]:
    """Return the cache key of each package: its build key, the same on all the architectures, then its release
    suffix, %autorev: the number of its build ID, ``+b<n>``, and its development build suffix of `autorev`, if any.

    See xcraft.buildid. A development build, or one built against development builds, has the hash of its build key
    and of the cache keys of the builds of ours it is built against rather than its build key, but a reused build. The
    `hashes` of the package directories are those of buildkey.directories(), computed now by default, and the build
    `ids` are those computed then, unless given, both from the `inputs` of the keys, unless given. The lock must be
    prepared: see resolution.prepare().
    """
    from xcraft import buildid  # noqa: PLC0415, which imports this module

    inputs = inputs or buildkey.Inputs(project, hashes)
    ids = ids if ids is not None else buildid.compute(project, buildid.base(project), inputs=inputs)
    # of all the architectures, like the build IDs: the same keys on all of them
    deps, autorev = inputs.deps, autorev or {}
    keys: dict[str, str] = {}

    def final(name: str) -> str:
        return f"{ids[name].key}+b{ids[name].number}"

    for component in components(deps):  # the dependencies first
        outside = {dep: keys[dep] for name in component for dep in deps[name] - component}
        # final builds against final builds only, whatever the suffixes of those built against development builds
        if not component & autorev.keys() and all(key == final(dep) for dep, key in outside.items()):
            keys |= {name: final(name) for name in component}
            continue
        builds = {name: [ids[name].key, autorev.get(name, "")] for name in component}
        for name in component:
            keys[name] = f"{buildkey.development_key(builds, outside, name)}+b{ids[name].number}{builds[name][1]}"
        # but a reused build, the main branch's, built against its builds
        keys |= {name: final(name) for name in component if ids[name].accepts}
    return keys


def release_suffix(key: str) -> str:
    """Return the release suffix of a build, %autorev, from its cache `key`: what follows the hash."""
    return key[64:]


def signed_build_dir(project: Project, fingerprint: str, name: str, key: str) -> Path:
    """Return where the build of `name` with `key` signed by the key `fingerprint` is cached."""
    return project.state / "cache/builds" / fingerprint / project.arch / name / key


def _cached_builds(project: Project, name: str, key: str = "*") -> list[Path]:
    """Return the cached builds of `name` with `key`, or all of them, whatever their signers, in progress included."""
    return sorted(project.state.glob(f"cache/builds/*/{project.arch}/{glob.escape(name)}/{key}"))


def cached_build(project: Project, name: str, key: str) -> Path:
    """Return where the build of `name` with `key` is cached: signed by the key of the command, or else by another,
    which the builds use as well, or where the command caches it, when none is.
    """
    own = signed_build_dir(project, project.signer.key if project.signer else "none", name, key)
    return own if own.exists() else next(iter(_cached_builds(project, name, key)), own)


def signed_build(project: Project, name: str, key: str, signer: Signer) -> Path:
    """Return the cached build of `name` with `key` signed by `signer`, signing a copy of the one of another key, if
    needed: a build is signed once by key, as signatures have a date, so that the storage has one of each.
    """
    signed = signed_build_dir(project, signer.key, name, key)
    if not signed.exists():
        if not (build := cached_build(project, name, key)).exists():
            msg = f"not built: {name}"
            raise BuildError(msg)
        work = signed.with_name(f".{key}")
        shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(build, work)  # a copy: signing changes the packages
        for output in ("RPMS", "SRPMS"):
            signer.sign_rpms(project, work / output)
        (work / KEY_FILE).write_text(signer.public_key())
        work.rename(signed)
    os.utime(signed)  # used, see xcraft clean --keep-days
    return signed


NOCHECK = "nocheck"
"""The file marking the builds without the tests of their specs in the cache, see untested()."""


def untested(project: Project, name: str, key: str) -> bool:
    """Return whether the cached build of `name` with `key` was made without the tests of its spec, %check, which the
    builds with them make again, and which is never pushed: a development build, or a final one cached by an xcraft
    giving --nocheck to the final builds too.
    """
    return (cached_build(project, name, key) / NOCHECK).exists()


def has_normal_build(project: Project, name: str) -> bool:
    """Return whether `name` has a cached build, of any version, besides the development builds."""
    # not a build in progress, nor a development one
    return any(
        not build.name.startswith(".") and not (build / "autorev").exists() for build in _cached_builds(project, name)
    )


def builds_repo(project: Project, keys: dict[str, str], names: Iterable[str] | None = None) -> LocalRepo:
    """Return the repository the builds run against, with the cached builds matching `keys`, of `names` only, or of
    all the packages.
    """
    repo = LocalRepo(project, project.state / "repo")
    repo.reset()
    for name in keys if names is None else names:
        if (build := cached_build(project, name, keys[name])).exists():
            os.utime(build)  # used, see xcraft clean --keep-days
            repo.add(build)
    return repo


def deps_repo(project: Project, name: str, keys: dict[str, str]) -> LocalRepo:
    """Return the repository the build of `name` runs against: the builds of its dependencies, with their `keys`,
    which its build root can have.
    """
    return builds_repo(project, keys, reachable(project.deps, project.deps[name]))


def build_path(project: Project, name: str, key: str, fingerprint: str) -> str:
    """Return the path of the tree of a build in the remote storage, signed by the key `fingerprint`."""
    return f"{INTERNAL}/builds/{fingerprint}/{project.arch}/{name}/{key}"


def build_signers(project: Project) -> list[str]:
    """Return the keys whose builds the remote storage gives, in order: the command's, or the developer's, then the
    trusted keys of the storages, `build_keys`.
    """
    own = project.signer or dev_signer(project)
    trusted = sorted({key for storage in project.config.remote.values() for key in storage.build_keys})
    return [own.key, *(k for k in trusted if k != own.key)] if own else trusted


BUILD_FILES = ["RPMS", "SRPMS", "build.log", "rpm-deps.json", KEY_FILE]
"""What a build is made of, in the cache and in the remote storage: what rpm generated for its binary packages too, see
checks.record_rpm_deps(), which a machine with the same builds of ours there does not resolve again. The builds pushed
before it was have none."""


def previous_duration(project: Project, name: str) -> float | None:
    """Return how long the last cached build of `name` took, if any."""
    builds = sorted((b / "build.log" for b in _cached_builds(project, name) if (b / "build.log").exists()),
                    key=lambda log: log.stat().st_mtime)
    for log in reversed(builds):
        # the build environment runs the build under `time`
        if times := re.findall(r"^real\s+(\d+)m([\d.]+)s$", log.read_text(errors="replace"), re.MULTILINE):
            minutes, seconds = times[-1]
            return int(minutes) * 60 + float(seconds)
    return None
