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
import shutil
from functools import cache
from typing import TYPE_CHECKING

from pydantic import BaseModel

from xcraft.container import BUILDENV, PACKAGES, base_packages, repo_file, run_natively
from xcraft.graph import reachable
from xcraft.localrepo import extra_rpms
from xcraft.lock import Locked, Pin, choose_versions, load_lock, save_lock
from xcraft.project import write_atomically
from xcraft.specs import dep_name

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xcraft.lock import Lock
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


def prepare(project: Project, *, roots: bool = True, repin: set[str] | None = None) -> None:
    """Lock the upstream source packages needed by the build environment image, the one of the host too, for the
    specs, and the build roots, unless not `roots`: what the commands resolving do first, and the builds once they
    recorded what they found.

    With the build roots, those of all the architectures are recorded in the lock, with their images: the build keys
    cover them, on any architecture. Their resolutions are cached, so only a change resolves them again. The binary
    packages of the source packages `repin` are pinned again, from the repositories as they are now, see _pins().
    """
    if repin:
        _forget(project)
    native, repinned = project.on_host(), frozenset(repin or ())
    images = [native] if native == project else [native, project]
    for platform in images:
        _locking(platform, _image)
    # pinned, for the images to be built: the one of the host parses the specs, which the build roots need
    _saved(project, lambda recorded, lock: _record_images(images, recorded, lock, repinned))
    if roots:
        for platform in project.all_platforms():
            _locking(platform, _image)
            _locking(platform, _roots)
        _record(project, repinned)


def _forget(project: Project) -> None:
    """Forget what was resolved, and the metadata of the repositories, to resolve from the repositories as they are."""
    _resolved.clear()
    shutil.rmtree(project.state / "cache/resolved", ignore_errors=True)
    for platform in project.all_platforms():
        run_natively(
            platform, "expire", ["dnf", "-q", "clean", "expire-cache"], volumes=[], show="forgetting the metadata"
        )


def _pins(
    arch: str, recorded: Lock, resolutions: list[Resolved], names: set[str], repin: frozenset[str]
) -> dict[str, Pin]:
    """Return the pinned binary packages `names` of `arch`, of the `resolutions`: those of the `recorded` lock, whatever
    the repositories have now, but those of the source packages `repin`, which xcraft lock --update pins again, and
    the new ones. Raise when a pinned package is resolved in another version or architecture: only an update pins it
    again.
    """
    resolved: dict[str, Pin] = {}
    for resolution in resolutions:
        for name, p in resolution.packages.items():
            pin = Pin(evr=p.evr, arch=p.arch, sha256=p.sha256, source=p.source)
            if resolved.setdefault(name, pin) != pin:
                msg = f"{name} resolved for {arch} as {resolved[name].text()}, and as {pin.text()}"
                raise ResolutionError(msg)
    pins = {}
    for name in sorted(names):
        old, new = recorded.binaries.get(arch, {}).get(name), resolved.get(name)
        if old and old.source not in repin:
            if new and (old.evr, old.arch, old.source) != (new.evr, new.arch, new.source):
                msg = (
                    f"{name} is pinned for {arch} as {old.text()}, but resolved as {new.text()}: xcraft lock --update "
                    f"{old.source} pins it again"
                )
                raise ResolutionError(msg)
            pins[name] = old
        elif new:
            pins[name] = new
        else:
            msg = f"{name} is neither pinned nor resolved for {arch}"
            raise ResolutionError(msg)
    return pins


def _saved(project: Project, record: Callable[[Lock, Lock], None]) -> None:
    """Change the lock as `record` does, from the lock as read, and to a copy of it, which is saved only when it did
    not change meanwhile, by another command: changed again from the new one otherwise.
    """
    for _ in range(3):
        recorded = load_lock(project)
        lock = recorded.model_copy(deep=True)
        record(recorded, lock)
        if lock == recorded:
            return
        with project.lock():
            if load_lock(project) == recorded:
                save_lock(project, lock)
                return
    msg = "the lock keeps changing, by other commands"
    raise ResolutionError(msg)


def _record_images(platforms: list[Project], recorded: Lock, lock: Lock, repin: frozenset[str]) -> None:
    """Record in the `lock` the upstream binary packages of the images of the `platforms`, pinned, see _pins(), with
    those the `recorded` lock pins for the build roots.
    """
    for platform in platforms:
        image = _locked(_image(platform))
        if error := image.errors.get("image"):
            msg = f"the build environment image cannot be resolved for {platform.arch}: {error}"
            raise ResolutionError(msg)
        lock.image[platform.arch] = sorted(image.image)
        pinned = _pins(platform.arch, recorded, [image], set(image.image), repin)
        lock.binaries[platform.arch] = {**recorded.binaries.get(platform.arch, {}), **pinned}


def _record(project: Project, repin: frozenset[str] = frozenset()) -> None:
    """Record in the lock the upstream binary packages of the images and of the build roots of all the architectures,
    pinned, see _pins(), and keep in it what the builds found for the packages and architectures of the project only.
    """
    platforms = project.all_platforms()

    @cache
    def binaries() -> set[str]:
        return {b.name for p in platforms for package in p.parsed.values() for b in package.binaries}

    def record(recorded: Lock, lock: Lock) -> None:
        lock.image, lock.binaries = {}, {}
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
            lock.image[platform.arch] = sorted(image.image)
            for name in sorted(project.package_dirs):
                # what the lock has for those that cannot be resolved, until they can
                values = (
                    recorded.roots.of(platform.arch).get(name, [])
                    if name in roots.errors
                    else sorted(set(roots.buildroots.get(name, [])) - {*image.image})
                )
                lock.roots.record(platform.arch, project.first, name, values)
            names = {*image.image, *(n for values in lock.roots.of(platform.arch).values() for n in values)}
            lock.binaries[platform.arch] = _pins(platform.arch, recorded, [image, roots], names, repin)

    _saved(project, record)


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
    data = {
        **_image_data(project),
        "record": {
            built: {"requires": parsed[name].requires, "provides": parsed[name].provides}
            for built, name in names.items()
        },
        "wanted": _wanted(project),
    }
    return _run(project, data, show="recording what rpm generated for the packages built", rpms=rpms)


def _wanted(project: Project) -> list[str]:
    """Return the capabilities our specs require, which the resolution of what rpm generated records the providers
    of.
    """
    return sorted({dep_name(r) for p in project.parsed.values() for r in p.requirements})


def rpm_deps_context(project: Project, builds: dict[str, str]) -> str:
    """Return the hash of what resolve_rpm_deps() resolves with, but the binary packages it records, which their
    builds cover: its script, the repositories, the request of _image_data(), with the base image by digest rather
    than its packages, the capabilities our specs require, the extra upstream RPMs, and the `builds` of ours it gets,
    by package and cache key.
    """
    config = project.config
    inputs = [
        _SCRIPT.read_text(), repo_file(project), project.arch, config.release_suffixes, load_lock(project).sources,
        config.base_image, sorted({*config.packages, *PACKAGES}), _wanted(project), config.extra_upstream, builds,
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()


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
