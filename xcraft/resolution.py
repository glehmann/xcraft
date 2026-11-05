"""The build environment image, the build roots, and what the published repositories and the tools making the ISO
need, of an architecture, resolved from the lock and the specs.

dnf resolves them without installing anything, in the base image of the host,
natively, for any architecture (see buildenv/resolve.py): the image from the
packages of the base image and those of the configuration, and the build root
of each package of ours from its BuildRequires, against the upstream
repositories, in the locked versions, and a repository of our binary packages,
written from their specs and what their builds found. So the build keys are
known before building, on every architecture, and what is published is locked
and pinned before publishing.

Resolving does not change the lock: prepare() does, for the commands, and the
builds once they recorded what they found, locking the upstream source packages
needed but not locked yet, in the latest version all the architectures have.
The results are cached by what they are resolved from.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from typing import TYPE_CHECKING

from pydantic import BaseModel
from rich.markup import escape

from xcraft import remote
from xcraft.builds import drop_gone
from xcraft.container import BUILDENV, PACKAGES, base_packages, repo_file, run_natively
from xcraft.display import console
from xcraft.graph import reachable
from xcraft.localrepo import extra_rpms
from xcraft.lock import Locked, Pin, cached_lock, choose_versions, load_lock, save_lock
from xcraft.packages import dep_name
from xcraft.project import write_atomically

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from xcraft.lock import Lock
    from xcraft.project import Project

_SCRIPT = BUILDENV / "resolve.py"
ISO_TOOLS = ["dosfstools", "memtest86+", "mtools", "syslinux-nonlinux", "xorriso"]
"""The packages of the tools making the ISO, which the image does not have."""


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
    runtime: list[str] = []
    """The upstream packages of the published repositories: what our packages, the extra ones, the installer's and
    those of the repository of the ISO need."""
    tools: list[str] = []
    """The upstream packages of the tools making the ISO, but the image's."""
    runtime_errors: dict[str, str] = {}
    """Why packages of the published repositories cannot be installed, by package, or group, like install.img or
    ISO."""
    tools_error: str = ""
    """Why the tools making the ISO cannot be installed."""

    def image_packages(self) -> dict[str, Locked]:
        """Return the upstream packages of the image."""
        return {name: self.packages[name] for name in self.image}


def _run(project: Project, data: dict, *, show: str, rpms: Sequence[Path] = ()) -> Resolved:
    """Return what resolve.py resolves from `data` for the platform of `project`, with the built `rpms`, in the cache,
    and the extra upstream RPMs.
    """
    cache = project.state / "cache"
    paths = sorted(f"/cache/{rpm.relative_to(cache)}" for rpm in [*rpms, *extra_rpms(project)])
    data = {"rpms": paths, "record": {}, "wanted": [], "versions": [], **data}
    if "base" not in data:  # by reference in the cache keys, see _cache()
        data["base"] = base_packages(project)
    with project.tmp_dir("resolve") as work:
        (work / "input.json").write_text(json.dumps(data))
        run_natively(
            project,
            "resolve",
            ["python3", "/xcraft/resolve.py", "/work/input.json", "/work/output.json"],
            volumes=[f"{work}:/work", f"{cache}:/cache:ro"],
            show=show,
        )
        return Resolved.model_validate_json((work / "output.json").read_text())


_resolved: dict[Path, Resolved] = {}
"""The cached resolutions read by this process, by file."""


def resolutions_read() -> set[Path]:
    """Return the files of the cached resolutions this process read: those of the lock as it is, once prepared."""
    return set(_resolved)


def _extra(project: Project) -> dict[str, dict[str, str]]:
    """Return the extra upstream RPMs of the project, which resolutions get, by name, as dicts."""
    return {name: extra.model_dump() for name, extra in project.config.extra_upstream.items()}


def _pinned(project: Project) -> dict[str, str]:
    """Return the pinned upstream packages of the architecture of the project: those of the source versions locked,
    as the repositories had them when pinned, which may publish other contents of the same versions since.
    """
    return {name: pin.text() for name, pin in cached_lock(project).binaries.get(project.arch, {}).items()}


def _cache(project: Project, data: dict) -> Path:
    """Return the cache of what resolve.py resolves from `data`, by its key: the script, the repositories, the
    request, with the base image by reference rather than its packages, which _run() reads, the extra upstream
    RPMs, which _run() gives it, like the build keys: by their URLs, and the pinned packages, which another content
    of the same versions, pinned again by xcraft lock --update, resolves again. The same on all the machines, for
    the remote storage, see remote_tree().
    """
    inputs = [
        _SCRIPT.read_text(),
        repo_file(project),
        data,
        project.config.base_image,
        _extra(project),
        _pinned(project),
    ]
    key = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode())
    return project.state / "cache/resolved" / project.arch / f"{key.hexdigest()}.json"


def remote_tree(cache: Path) -> str:
    """Return the tree of the remote storage of the cached resolution `cache`, which has it as is."""
    return f"{remote.RESOLVED}/{cache.parent.name}/{cache.stem}"


_unreachable: set[Path] = set()
"""The projects whose remote storage failed, or which resolve from the repositories as they are, see _forget(): not
asked again by this process."""


def _fetched(project: Project, cache: Path) -> bool:
    """Download the resolution `cache` from the remote storage, signed by a trusted key, and return whether it has it:
    pushed by CI, see xcraft push. A storage failing is warned about once, and resolving goes on without it.
    """
    if not project.config.remote or project.root in _unreachable:
        return False
    tree = cache.parent / f".{cache.stem}-{os.getpid()}"
    try:
        if found := remote.fetch_tree(project, remote_tree(cache), tree):
            (tree / cache.name).rename(cache)
    except (remote.RemoteError, OSError) as e:
        _unreachable.add(project.root)
        console.print(f"[yellow]no resolutions from the remote storage[/]: {escape(str(e))}")
        return False
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    return found


def _cached(project: Project, data: dict, *, show: str) -> Resolved:
    """Return what resolve.py resolves from `data`, from the cache when it was resolved before, here or by CI."""
    if (cache := _cache(project, data)) not in _resolved:
        cache.parent.mkdir(parents=True, exist_ok=True)
        if not cache.exists() and not _fetched(project, cache):
            resolved = _run(project, data, show=show)
            write_atomically(cache, resolved.model_dump_json())  # for the commands reading it at once
        _resolved[cache] = Resolved.model_validate_json(cache.read_text())
    return _resolved[cache]


def _image_data(project: Project) -> dict:
    config = project.config
    return {
        "arch": project.arch,
        "suffixes": config.release_suffixes,
        "sources": cached_lock(project).sources,
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


def _locked(resolved: Resolved) -> Resolved:
    if resolved.unlocked:
        msg = f"upstream source packages not locked yet: {', '.join(resolved.unlocked)}"
        raise ResolutionError(msg)
    return resolved


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
    packages, deps = project.packages, project.spec_deps
    builds = {
        name: {
            "buildrequires": package.buildrequires,
            "allowed": sorted(b.name for dep in reachable(deps, deps[name]) for b in packages[dep].binaries),
        }
        for name, package in sorted(packages.items())
    }
    config = project.config
    # the ISOs are made for x86_64 only, for their boot loaders
    iso = config.iso.installer and project.arch.startswith("x86_64")
    runtime = {
        "extra": config.publish.extra,
        "installer": config.iso.installer,
        "iso": config.iso.packages if iso else [],
        "tools": ISO_TOOLS if iso else [],
    }
    return {**_image_data(project), "ours": _ours(project), "builds": builds, "runtime": runtime}


def _roots(project: Project) -> Resolved:
    return _cached(
        project, _roots_data(project), show=f"resolving the build roots of {project.arch}, and what is published"
    )


def resolve(project: Project) -> Resolved:
    """Return the build environment image and the build roots of the platform.

    Raise when the lock misses upstream source packages they need: prepare() it first.
    """
    resolve_image(project)
    return _locked(_roots(project))


def _locking(project: Project, resolve: Callable[[Project], Resolved]) -> None:
    """Lock the upstream source packages what `resolve` resolves for `project` needs."""
    for _ in (1, 2, 3):
        if not (resolved := resolve(project)).unlocked:
            return
        lock_sources(project, resolved.unlocked)
    msg = f"upstream source packages needed, but locked in a version {project.arch} does not have: "
    raise ResolutionError(msg + ", ".join(resolved.unlocked))


def _forget(project: Project) -> None:
    """Forget what was resolved, and the metadata of the repositories, to resolve from the repositories as they are,
    rather than from the resolutions of the remote storage, until the end of the command.
    """
    _resolved.clear()
    _unreachable.add(project.root)
    shutil.rmtree(project.state / "cache/resolved", ignore_errors=True)
    for platform in project.all_platforms():
        run_natively(
            platform, "expire", ["dnf", "-q", "clean", "expire-cache"], volumes=[], show="forgetting the metadata"
        )


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


def _record_published(recorded: Lock, lock: Lock, arch: str, roots: Resolved) -> None:
    """Record in the `lock` the upstream binary packages of the published repositories and of the tools making the ISO
    of `arch`, as resolved with the build `roots`, each as `recorded` until it can be resolved.
    """
    for field, values, failed in (
        ("runtime", roots.runtime, roots.runtime_errors),
        ("iso", roots.tools, roots.tools_error),
    ):
        if values := getattr(recorded, field).get(arch, []) if failed else values:
            getattr(lock, field)[arch] = values


def _record(project: Project, repin: frozenset[str] = frozenset()) -> None:
    """Record in the lock the upstream binary packages of the images, of the build roots, of the published
    repositories and of the tools making the ISO, of all the architectures, pinned, see _pins(), with the versions of
    their source packages only, and keep in the lock of the builds what the builds found for the packages of the
    project only.
    """
    platforms = project.all_platforms()
    drop_gone(project)

    def record(recorded: Lock, lock: Lock) -> None:
        lock.image, lock.binaries, lock.runtime, lock.iso = {}, {}, {}, {}
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
            _record_published(recorded, lock, platform.arch, roots)
            names = {
                *image.image, *(n for values in lock.roots.of(platform.arch).values() for n in values),
                *lock.runtime.get(platform.arch, []), *lock.iso.get(platform.arch, []),
            }  # fmt: skip
            lock.binaries[platform.arch] = _pins(platform.arch, recorded, [image, roots], names, repin)
        # those of no pin left, like those of a package published no more, in their latest version when needed again
        used = {pin.source for pins in lock.binaries.values() for pin in pins.values()}
        lock.sources = {source: version for source, version in lock.sources.items() if source in used}

    _saved(project, record)


def prepare(project: Project, *, roots: bool = True, repin: set[str] | None = None) -> None:
    """Lock the upstream source packages needed by the build environment image, the one of the host too, for the
    specs, and the build roots, unless not `roots`: what the commands resolving do first, and the builds once they
    recorded what they found.

    With the build roots, those of all the architectures are recorded in the lock, with their images, and what is
    published and the tools making the ISO: the build keys cover them, on any architecture, and publishing gets them
    pinned. Their resolutions are cached, so only a change resolves them again. The binary
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
    project.refresh()  # for the build roots recorded


def published(project: Project) -> list[str]:
    """Return the upstream packages of the published repositories of the platform, which prepare() pinned in the lock.

    Raise when some of the packages published cannot be installed.
    """
    if errors := resolve(project).runtime_errors:
        for name, error in sorted(errors.items()):
            console.print(f"[red]✗[/] {escape(f'{name}: {error}')}")
        msg = f"cannot install, on {project.arch}: {', '.join(sorted(errors))}"
        raise ResolutionError(msg)
    return cached_lock(project).runtime.get(project.arch, [])


def _wanted(project: Project) -> list[str]:
    """Return the capabilities our specs require, which the resolution of what rpm generated records the providers
    of.
    """
    return sorted({dep_name(r) for p in project.parsed.values() for r in p.requirements})


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


def rpm_deps_context(project: Project, builds: dict[str, str]) -> str:
    """Return the hash of what resolve_rpm_deps() resolves with, but the binary packages it records, which their
    builds cover: its script, the repositories, the request of _image_data(), the pinned packages, with the base
    image by digest rather than its packages, the capabilities our specs require, the extra upstream RPMs, and the
    `builds` of ours it gets, by package and cache key.
    """
    config = project.config
    inputs = [
        _SCRIPT.read_text(), repo_file(project), project.arch, config.release_suffixes, cached_lock(project).sources,
        _pinned(project), config.base_image, sorted({*config.packages, *PACKAGES}), _wanted(project), _extra(project),
        builds,
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


def _lock_installed(project: Project, installed: dict[str, Locked]) -> bool:
    """Lock the upstream source packages of the `installed` packages not locked yet, and return whether all of them
    were installed in the locked versions: those locked here may get other ones, which the other architectures have,
    and the packages have to be installed again.

    Raise when locked ones were installed in another version: not from the repository of the pinned packages.
    """

    def changed() -> list[str]:
        """Return the installed packages of locked sources not in their locked versions, or not in them at all."""
        locked, sources = resolve_image(project).packages, cached_lock(project).sources
        return sorted(
            n
            for n, p in installed.items()
            if ((locked[n].evr, locked[n].arch) != (p.evr, p.arch) if n in locked else p.source in sources)
        )

    if before := changed():
        msg = f"packages installed in another version than locked: {', '.join(before)}"
        raise ResolutionError(msg)
    if new := sorted({p.source for p in installed.values()} - cached_lock(project).sources.keys()):
        lock_sources(project, new)
        return not changed()
    return True
