"""The build environment image and the build roots of an architecture, resolved from the lock and the specs.

dnf resolves them without installing anything, in the base image of the host,
natively, for any architecture (see buildenv/resolve.py): the image from the
packages of the base image and those of the configuration, and the build root
of each package of ours from its BuildRequires, against the upstream
repositories, in the locked versions, and a repository of our binary packages,
written from their specs and what their builds found. So the build keys are
known before building, on every architecture.

The upstream source packages they need that are not locked yet are locked, in
the latest version all the architectures have. The results are cached by what
they are resolved from.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel

from xcraft.container import BUILDENV, PACKAGES, base_packages, repo_file, run_natively
from xcraft.graph import reachable
from xcraft.localrepo import extra_rpms
from xcraft.lock import Locked, choose_versions, load_lock, save_lock
from xcraft.specs import dep_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xcraft.project import Project

_SCRIPT = BUILDENV / "resolve.py"


class ResolutionError(RuntimeError):
    """Raised when the image cannot be resolved, or the upstream packages needed cannot be locked."""


class Resolved(BaseModel):
    """What dnf resolved for an architecture."""

    packages: dict[str, Locked] = {}
    """The upstream binary packages of the locked source packages, in their locked versions."""
    image: list[str] = []
    """The upstream packages of the build environment image."""
    buildroots: dict[str, list[str]] = {}
    """The upstream packages of the build root of each of our packages, the image's included."""
    ours: dict[str, list[str]] = {}
    """The binary packages of ours of the build root of each of our packages, and the extra upstream ones."""
    errors: dict[str, str] = {}
    """Why packages of ours, or the image, cannot be resolved."""
    unlocked: list[str] = []
    """The upstream source packages needed, not locked yet."""
    versions: dict[str, list[str]] = {}
    """The available versions of upstream source packages, oldest first."""
    requires: dict[str, list[str]] = {}
    """For the binary packages of ours recorded, the packages their generated requirements resolve to."""
    provides: dict[str, list[str]] = {}
    """For the binary packages of ours recorded, their generated capabilities that our specs require."""

    def image_packages(self) -> dict[str, Locked]:
        """Return the upstream packages of the image."""
        return {name: self.packages[name] for name in self.image}


def _run(project: Project, data: dict, *, show: str, rpms: list[Path] = []) -> Resolved:  # noqa: B006
    """Return what resolve.py resolves from `data` for the platform of `project`, with the built `rpms`, in the cache,
    and the extra upstream RPMs.
    """  # noqa: D205
    cache = project.state / "cache"
    paths = sorted(f"/cache/{rpm.relative_to(cache)}" for rpm in [*rpms, *extra_rpms(project)])
    data = {"rpms": paths, "record": {}, "wanted": [], "versions": [], **data}
    with project.work("resolve") as work:
        (work / "input.json").write_text(json.dumps(data))
        run_natively(
            project,
            "resolve",
            ["python3", "/xcraft/resolve.py", "/work/input.json", "/work/output.json"],
            volumes=[f"{work}:/work", f"{cache}:/cache:ro"],
            show=show,
        )
        return Resolved.model_validate_json((work / "output.json").read_text())


_resolved: dict[str, Resolved] = {}


def _cached(project: Project, data: dict, *, show: str) -> Resolved:
    """Return what resolve.py resolves from `data`, from the cache when it was resolved before.

    The cache key covers the extra upstream RPMs too, which _run() gives it, like the build keys: by their URLs.
    """
    inputs = [_SCRIPT.read_text(), repo_file(project), data, project.config.extra_upstream]
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode())
    cache = project.state / "cache/resolved" / project.arch / f"{key.hexdigest()}.json"
    if cache.name not in _resolved:
        if not cache.exists():
            resolved = _run(project, data, show=show)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(resolved.model_dump_json())
        _resolved[cache.name] = Resolved.model_validate_json(cache.read_text())
    return _resolved[cache.name]


def _image_data(project: Project) -> dict:
    config = project.config
    return {
        "arch": project.arch,
        "suffixes": config.release_suffixes,
        "sources": load_lock(project).sources,
        "base": base_packages(project),
        "image": sorted({*config.packages, *PACKAGES}),
        "ours": [],
        "builds": {},
    }


def available_versions(project: Project, sources: list[str]) -> dict[str, list[str]]:
    """Return the available versions of the upstream source packages `sources` on the platform, oldest first."""
    data = {**_image_data(project), "base": [], "image": [], "versions": sources}
    return _run(project, data, show=f"looking for the versions of the upstream packages of {project.arch}").versions


def lock_sources(project: Project, sources: list[str]) -> None:
    """Lock the upstream source packages `sources`, in the latest version all the architectures have."""
    if project.frozen:
        msg = f"upstream packages not locked, which --locked forbids: {', '.join(sorted(sources))}"
        raise ResolutionError(msg)
    chosen = choose_versions({p.arch: available_versions(p, sources) for p in project.all_platforms()})
    if missing := sorted(set(sources) - chosen.keys()):
        msg = f"upstream source packages found on no architecture: {', '.join(missing)}"
        raise ResolutionError(msg)
    lock = load_lock(project)
    lock.sources |= chosen
    save_lock(project, lock)
    project.refresh()


def _locking(project: Project, resolve: Callable[[], Resolved]) -> Resolved:
    """Return what `resolve` resolves, once the upstream source packages it needs are locked."""
    for _ in range(3):
        resolved = resolve()
        if not resolved.unlocked:
            return resolved
        lock_sources(project, resolved.unlocked)
    msg = f"upstream source packages needed, but locked in a version {project.arch} does not have: "
    raise ResolutionError(msg + ", ".join(resolved.unlocked))


def resolve_image(project: Project) -> Resolved:
    """Return the packages of the build environment image of the platform, and the locked upstream packages."""
    resolved = _locking(
        project, lambda: _cached(project, _image_data(project), show=f"resolving the image of {project.arch}")
    )
    if error := resolved.errors.get("image"):
        msg = f"the build environment image of {project.arch} cannot be resolved: {error}"
        raise ResolutionError(msg)
    return resolved


def _ours(project: Project) -> list[dict]:
    """Return our binary packages, for the repository of resolve.py."""
    return [
        {**binary.model_dump(), "package": name}
        for name, package in sorted(project.packages.items())
        for binary in package.binaries
    ]


def resolve(project: Project) -> Resolved:
    """Return the build environment image and the build roots of the platform."""

    def data() -> dict:
        packages, deps = project.packages, project.deps
        builds = {
            name: {
                "buildrequires": package.buildrequires,
                "allowed": sorted(b.name for dep in reachable(deps, deps[name]) for b in packages[dep].binaries),
            }
            for name, package in sorted(packages.items())
        }
        return {**_image_data(project), "ours": _ours(project), "builds": builds}

    resolve_image(project)
    return _locking(project, lambda: _cached(project, data(), show=f"resolving the build roots of {project.arch}"))


def record(project: Project, names: list[str], rpms: list[Path]) -> Resolved:
    """Return the packages the requirements generated by rpm of the binary packages of ours `names` resolve to, and
    their generated capabilities that our specs require, from the built `rpms`, theirs and those providing them.
    """  # noqa: D205
    parsed = {b.name: b for package in project.parsed.values() for b in package.binaries}
    wanted = sorted(
        {
            dep_name(r)
            for p in project.parsed.values()
            for r in [*p.buildrequires, *(r for b in p.binaries for r in b.requires)]
        }
    )
    data = {
        **_image_data(project),
        "record": {name: {"requires": parsed[name].requires, "provides": parsed[name].provides} for name in names},
        "wanted": wanted,
    }
    return _run(project, data, show="recording what rpm generated for the packages built", rpms=rpms)


def upstream_sources(project: Project, names: list[str]) -> list[str]:
    """Return the locked upstream source packages of `names`, binary or source names."""
    lock = load_lock(project)
    binaries = {
        n: p.source
        for platform in (project.all_platforms() if set(names) - lock.sources.keys() else [])
        for n, p in resolve_image(platform).packages.items()
    }
    if unknown := sorted(n for n in names if n not in lock.sources and n not in binaries):
        msg = f"not locked: {', '.join(unknown)}"
        raise ResolutionError(msg)
    return sorted({n if n in lock.sources else binaries[n] for n in names})


def lock_installed(project: Project, installed: dict[str, Locked]) -> None:
    """Lock the upstream source packages of the `installed` packages not locked yet.

    Raise when locked ones were installed in another version, which means the versionlock was not applied.
    """
    locked = resolve_image(project).packages
    if changed := sorted(
        n for n, p in installed.items() if n in locked and (locked[n].evr, locked[n].arch) != (p.evr, p.arch)
    ):
        msg = f"packages installed in another version than locked: {', '.join(changed)}"
        raise ResolutionError(msg)
    if new := sorted({p.source for p in installed.values()} - load_lock(project).sources.keys()):
        lock_sources(project, new)
