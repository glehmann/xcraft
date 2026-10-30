"""The build environment image and the build roots of an architecture, resolved from the lock and the specs.

dnf resolves them without installing anything, in the base image of the host,
natively, for any architecture (see buildenv/resolve.py): the image from the
packages of the base image and those of the configuration, and the build root
of each package of ours from its BuildRequires, against the upstream
repositories, in the locked versions, and a repository of our binary packages,
written from their specs and what their builds found. So the build keys are
known before building, on every architecture.

Resolving does not change the lock: prepare() does, for the commands, and the
builds once they recorded what they found, locking the upstream source packages
needed but not locked yet, in the latest version all the architectures have.
The results are cached by what they are resolved from.
"""

from __future__ import annotations

import hashlib
import json
from functools import cache
from typing import TYPE_CHECKING

from pydantic import BaseModel

from xcraft.container import BUILDENV, PACKAGES, base_packages, repo_file, run_natively
from xcraft.graph import reachable
from xcraft.localrepo import extra_rpms
from xcraft.lock import Locked, choose_versions, load_lock, save_lock
from xcraft.project import write_atomically
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
    """
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
            write_atomically(cache, resolved.model_dump_json())  # for the commands reading it at once
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
    """Lock the upstream source packages `sources`, in the latest version all the architectures have.

    It holds the lock of the project to change the lock file, which commands only reading the project, like xcraft
    status, may need to do.
    """
    if project.frozen:
        msg = f"upstream packages not locked, which --locked forbids: {', '.join(sorted(sources))}"
        raise ResolutionError(msg)
    chosen = choose_versions({p.arch: available_versions(p, sources) for p in project.all_platforms()})
    if missing := sorted(set(sources) - chosen.keys()):
        msg = f"upstream source packages found on no architecture: {', '.join(missing)}"
        raise ResolutionError(msg)
    with project.lock():
        lock = load_lock(project)
        # those locked meanwhile, by another command, keep their versions
        lock.sources |= {source: version for source, version in chosen.items() if source not in lock.sources}
        save_lock(project, lock)
    project.refresh()


def _locking(project: Project, resolve: Callable[[Project], Resolved]) -> None:
    """Lock the upstream source packages what `resolve` resolves for `project` needs."""
    for _ in range(3):
        if not (resolved := resolve(project)).unlocked:
            return
        lock_sources(project, resolved.unlocked)
    msg = f"upstream source packages needed, but locked in a version {project.arch} does not have: "
    raise ResolutionError(msg + ", ".join(resolved.unlocked))


def _locked(resolved: Resolved) -> Resolved:
    if resolved.unlocked:
        msg = f"upstream source packages not locked yet: {', '.join(resolved.unlocked)}"
        raise ResolutionError(msg)
    return resolved


def prepare(project: Project, *, roots: bool = True) -> None:
    """Lock the upstream source packages needed by the build environment image, the one of the host too, for the
    specs, and the build roots, unless not `roots`: what the commands resolving do first, and the builds once they
    recorded what they found.

    With the build roots, those of all the architectures are recorded in the lock, with their images: the build keys
    cover them, on any architecture. Their resolutions are cached, so only a change resolves them again.
    """
    native = project.on_host()
    for platform in [native] if native == project else [native, project]:
        _locking(platform, _image)
    if roots:
        for platform in project.all_platforms():
            _locking(platform, _image)
            _locking(platform, _roots)
        _record(project)


def _record(project: Project) -> None:
    """Record in the lock the upstream source packages of the images and of the build roots of all the architectures,
    and keep in it what the builds found for the packages and architectures of the project only.

    They are resolved from the lock as read, and saved only when it did not change meanwhile, by another command: they
    are resolved again from the new one otherwise.
    """
    platforms = project.all_platforms()

    @cache
    def binaries() -> set[str]:
        return {b.name for p in platforms for package in p.parsed.values() for b in package.binaries}

    for _ in range(3):
        recorded = load_lock(project)
        lock = recorded.model_copy(deep=True)
        lock.image = {}
        for kept in (lock.requires, lock.provides):
            if kept.shared or kept.arches:  # the specs parsed only then
                kept.keep(binaries())
        lock.buildrequires.keep({*project.package_dirs})
        lock.roots.keep({*project.package_dirs})
        lock.roots.arches = {}
        for platform in platforms:
            image, roots = _locked(_image(platform)), _locked(_roots(platform))
            if error := image.errors.get("image"):
                msg = f"the build environment image cannot be resolved for {platform.arch}: {error}"
                raise ResolutionError(msg)
            lock.image[platform.arch] = sorted({image.packages[n].source for n in image.image})
            for name in sorted(project.package_dirs):
                # what the lock has for those that cannot be resolved, until they can
                values = (
                    recorded.roots.of(platform.arch).get(name, [])
                    if name in roots.errors
                    else sorted(
                        {roots.packages[n].source for n in roots.buildroots.get(name, [])}
                        - {*lock.image[platform.arch]}
                    )
                )
                lock.roots.record(platform.arch, project.first, name, values)
        if lock == recorded:
            return
        with project.lock():
            if load_lock(project) == recorded:
                save_lock(project, lock)
                return
    msg = "the lock keeps changing, by other commands"
    raise ResolutionError(msg)


def _image(project: Project) -> Resolved:
    return _cached(project, _image_data(project), show=f"resolving the image of {project.arch}")


def resolve_image(project: Project) -> Resolved:
    """Return the packages of the build environment image of the platform, and the locked upstream packages.

    Raise when the lock misses some: prepare() it first.
    """
    resolved = _locked(_image(project))
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


def _roots_data(project: Project) -> dict:
    packages, deps = project.packages, project.deps
    builds = {
        name: {
            "buildrequires": package.buildrequires,
            "allowed": sorted(b.name for dep in reachable(deps, deps[name]) for b in packages[dep].binaries),
        }
        for name, package in sorted(packages.items())
    }
    return {**_image_data(project), "ours": _ours(project), "builds": builds}


def _roots(project: Project) -> Resolved:
    return _cached(project, _roots_data(project), show=f"resolving the build roots of {project.arch}")


def resolve(project: Project) -> Resolved:
    """Return the build environment image and the build roots of the platform.

    Raise when the lock misses upstream source packages they need: prepare() it first.
    """
    resolve_image(project)
    return _locked(_roots(project))


def resolve_rpm_deps(project: Project, names: dict[str, str], rpms: list[Path]) -> Resolved:
    """Return the packages the requirements generated by rpm of the binary packages of ours `names` resolve to, and
    their generated capabilities that our specs require, from the built `rpms`, theirs and those providing them. The
    `names` are those of the binary packages as built, by their names in the specs, which differ when they include
    the release, like the livepatch packages of the kernel; the result is by the names as built.
    """
    parsed = {b.name: b for package in project.parsed.values() for b in package.binaries}
    wanted = sorted({dep_name(r) for p in project.parsed.values() for r in p.requirements})
    data = {
        **_image_data(project),
        "record": {
            built: {"requires": parsed[name].requires, "provides": parsed[name].provides}
            for built, name in names.items()
        },
        "wanted": wanted,
    }
    return _run(project, data, show="recording what rpm generated for the packages built", rpms=rpms)


def upstream_sources(project: Project, names: list[str]) -> list[str]:
    """Return the locked upstream source packages of `names`, binary or source names, which the images of all the
    architectures are prepared for, to know the binary ones.
    """
    lock = load_lock(project)
    platforms = project.all_platforms() if set(names) - lock.sources.keys() else []
    for platform in platforms:
        prepare(platform, roots=False)
    binaries = {n: p.source for platform in platforms for n, p in resolve_image(platform).packages.items()}
    if unknown := sorted(n for n in names if n not in lock.sources and n not in binaries):
        msg = f"not locked: {', '.join(unknown)}"
        raise ResolutionError(msg)
    return sorted({n if n in lock.sources else binaries[n] for n in names})


def lock_installed(project: Project, installed: dict[str, Locked]) -> bool:
    """Lock the upstream source packages of the `installed` packages not locked yet, and return whether all of them
    were installed in the locked versions: those locked here may get other ones, which the other architectures have,
    and the packages have to be installed again.

    Raise when locked ones were installed in another version, which means the versionlock was not applied.
    """

    def changed() -> list[str]:
        """Return the installed packages of locked sources not in their locked versions, or not in them at all."""
        locked, sources = resolve_image(project).packages, load_lock(project).sources
        return sorted(
            n
            for n, p in installed.items()
            if ((locked[n].evr, locked[n].arch) != (p.evr, p.arch) if n in locked else p.source in sources)
        )

    if before := changed():
        msg = f"packages installed in another version than locked: {', '.join(before)}"
        raise ResolutionError(msg)
    if new := sorted({p.source for p in installed.values()} - load_lock(project).sources.keys()):
        lock_sources(project, new)
        return not changed()
    return True
