"""xcraft bump."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer

from xcraft import process
from xcraft.commands import Packages, load
from xcraft.display import activity, console, download_progress
from xcraft.importer import git
from xcraft.lock import load_lock, lock_path, save_lock
from xcraft.project import write_atomically
from xcraft.release import add_changelog_entry, bump_release, reset_release, set_version
from xcraft.sources import archive_name, cache_source, read_manifest, write_manifest
from xcraft.specs import load_packages, main_spec

if TYPE_CHECKING:
    from xcraft.project import Project
    from xcraft.specs import Package

Dep = Annotated[
    list[str] | None,
    typer.Option(
        "--dep",
        help="Upstream package, binary or source name: select the packages with a pending update of it.",
        show_default=False,
    ),
]


def pending_packages(names: list[str] | None, pending: dict[str, dict[str, str]], upstream: list[str]) -> list[str]:
    """Return the packages `names`, and those with pending updates of the `upstream` packages."""
    selected = set(names or []) | {package for package, entries in pending.items() if set(upstream) & entries.keys()}
    if not selected:
        msg = "no package given, or pending on the given upstream packages"
        raise typer.BadParameter(msg)
    return sorted(selected)


def _evr(package: Package) -> str:
    return package.binaries[0].evr.removesuffix(package.dist) if package.binaries else "?"


def same_version(groups: list[list[str]], name: str) -> list[str]:
    """Return `name` and the packages of the same version, from the groups of same_version in xcraft.toml."""
    return sorted({name, *(other for group in groups if name in group for other in group)})


def _new_archives(project: Project, before: dict[str, Package], after: dict[str, Package], names: list[str]) -> None:
    """Download the archives of the new Source0 of `names`, and put them in their sources manifests."""
    digests: dict[str, str] = {}
    for name in names:
        if "://" not in (url := after[name].source0):
            msg = f"the Source0 of {name} is not a URL, but {url or 'nothing'}: give the URL of its archive in its spec"
            raise ValueError(msg)
        if url not in digests:
            work = project.state / "tmp" / "archive"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir(parents=True)
            with activity(f"downloading {url}") as on_line:
                process.download(url, work / archive_name(url), download_progress(on_line))
            digests[url] = cache_source(project.source_cache, work / archive_name(url))
            shutil.rmtree(work)
        # the file of a link, like the sources of kernel-headers, which are the kernel's
        manifest = (project.package_dirs[name] / "sources").resolve()
        entries = read_manifest(manifest) if manifest.exists() else {}
        entries.pop(archive_name(before[name].source0), None)
        write_manifest(manifest, {**entries, archive_name(url): digests[url]})


def _update_specs(  # noqa: PLR0913
    project: Project, before: dict[str, Package], names: list[str], *, version: str, author: str, message: str
) -> dict[str, Package]:
    """Bump the specs of `names`, or set their `version`, with a changelog entry, and return the packages after.

    The specs and the sources manifests are written back as they were on errors.
    """
    specs = {package: main_spec(project.package_dirs[package]) for package in names}
    texts = {package: spec.read_text() for package, spec in specs.items()}
    bumped = {
        package: reset_release(set_version(text, version)) if version else bump_release(text)
        for package, text in texts.items()
    }
    manifests = {
        path: path.read_text()
        for name in names
        if (path := (project.package_dirs[name] / "sources").resolve()).exists()
    }
    try:
        for package, spec in specs.items():
            write_atomically(spec, bumped[package])
        # parse them again, for their new releases with their macros expanded
        after = load_packages(project)
        if version:
            _new_archives(project, before, after, names)
        day = datetime.now(UTC).date()
        for package, spec in specs.items():
            write_atomically(
                spec,
                add_changelog_entry(bumped[package], evr=_evr(after[package]), author=author, message=message, day=day),
            )
    except BaseException:
        for package, spec in specs.items():
            write_atomically(spec, texts[package])
        for path, text in manifests.items():
            write_atomically(path, text)
        raise
    return after


def bump(
    packages: Packages = None,
    dep: Dep = None,
    message: Annotated[
        str,
        typer.Option(
            "--message",
            "-m",
            help="Changelog entry: Update to <version> with --version, empty to fill in the spec otherwise.",
            show_default=False,
        ),
    ] = "",
    version: Annotated[
        str,
        typer.Option(
            "--version",
            help="New version of the package, and of those of the same version in xcraft.toml, with the archive of "
            "their Source0, which must be a URL, and 1 as their release.",
            show_default=False,
        ),
    ] = "",
) -> None:
    """Bump the release of packages, with a changelog entry by the git user, and clear their pending updates."""
    if version and (dep or len(packages or []) != 1):
        msg = "--version needs one package, without --dep"
        raise typer.BadParameter(msg)
    project = load(packages)
    before = project.packages
    name, email = (git("-C", str(project.root), "config", key) for key in ("user.name", "user.email"))
    author = f"{name} <{email}>"
    with project.lock():
        # the pending updates of all the platforms, which the next releases are built for
        locks = [(p, load_lock(p)) for p in project.all_platforms() if lock_path(p).exists()]
        pending: dict[str, dict[str, str]] = {}
        for _, lock in locks:
            for package, entries in lock.pending.items():
                pending.setdefault(package, {}).update(entries)
        if version:
            names = same_version(project.config.same_version, (packages or [])[0])
        else:
            deps = sorted({n for _, lock in locks for n in lock.binaries(dep or [])})
            names = pending_packages(packages, pending, deps)
        message = message or (f"Update to {version}" if version else "")
        after = _update_specs(project, before, names, version=version, author=author, message=message)
        for package in names:
            console.print(f"[bold]{package}[/]: {_evr(before[package])} -> {_evr(after[package])}")
        # their next releases are built with the current locks
        for on_platform, lock in locks:
            lock.clear_pending(names)
            save_lock(on_platform, lock)
    if version:
        console.print(f"once built, share the new archives with: xcraft push sources {' '.join(names)}")
