"""xcraft bump."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated

import typer
from rpm_version import evr_compare  # ty: ignore[unresolved-import]

from xcraft import process
from xcraft.commands import Packages, load
from xcraft.display import activity, console, download_progress
from xcraft.process import git
from xcraft.project import write_atomically
from xcraft.release import add_changelog_entry, bump_release, reset_release, set_version
from xcraft.sources import archive_name, cache_source, read_manifest, write_manifest
from xcraft.specs import load_packages, main_spec

if TYPE_CHECKING:
    from xcraft.project import Project
    from xcraft.specs import Package


def _check_parsed(packages: dict[str, Package], names: list[str]) -> None:
    """Raise when the specs of `names` cannot be parsed."""
    if errors := [f"{name}: {packages[name].error}" for name in names if packages[name].error]:
        msg = f"the specs cannot be parsed: {'; '.join(errors)}"
        raise RuntimeError(msg)


def _evr(package: Package) -> str:
    return package.binaries[0].evr.removesuffix(package.dist) if package.binaries else "?"


def same_version(groups: list[list[str]], name: str) -> list[str]:
    """Return `name` and the packages of the same version, from the groups of same_version in xcraft.toml."""
    return sorted({name, *(other for group in groups if name in group for other in group)})


def _check_version(packages: dict[str, Package], names: list[str], version: str) -> None:
    """Require a newer RPM version for every package being updated."""
    _check_parsed(packages, names)
    for name in names:
        if evr_compare(version, packages[name].version) <= 0:
            msg = f"{name}: --version {version} must be greater than current version {packages[name].version}"
            raise ValueError(msg)


def _new_archives(project: Project, before: dict[str, Package], after: dict[str, Package], names: list[str]) -> None:
    """Download the archives of the new Source0 of `names`, and put them in their sources manifests."""
    digests: dict[str, str] = {}
    for name in names:
        if "://" not in (url := after[name].source0):
            msg = f"the Source0 of {name} is not a URL, but {url or 'nothing'}: give the URL of its archive in its spec"
            raise ValueError(msg)
        if url not in digests:
            with project.work("archive") as work, activity(f"downloading {url}") as on_line:
                process.download(url, work / archive_name(url), download_progress(on_line))
                digests[url] = cache_source(project.source_cache, work / archive_name(url))
        # the file of a link, like the sources of kernel-headers, which are the kernel's
        manifest = (project.package_dirs[name] / "sources").resolve()
        entries = read_manifest(manifest) if manifest.exists() else {}
        entries.pop(archive_name(before[name].source0), None)
        write_manifest(manifest, {**entries, archive_name(url): digests[url]})


def _update_specs(
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
    # and those to remove, which a new version would create
    manifests = {
        path: path.read_text() if path.exists() else None
        for name in names
        if (path := (project.package_dirs[name] / "sources").resolve())
    }
    try:
        for package, spec in specs.items():
            write_atomically(spec, bumped[package])
        # parse them again, for their new releases with their macros expanded
        after = load_packages(project)
        _check_parsed(after, names)
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
            if text is None:
                path.unlink(missing_ok=True)
            else:
                write_atomically(path, text)
        raise
    return after


def bump(
    packages: Packages = None,
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
    """Bump the release of packages, with a changelog entry by the git user."""
    if not packages:
        msg = "no package given"
        raise typer.BadParameter(msg)
    if version and len(packages) != 1:
        msg = "--version needs one package"
        raise typer.BadParameter(msg)
    project = load(packages)
    before = project.packages
    name, email = (git("-C", str(project.root), "config", key) for key in ("user.name", "user.email"))
    author = f"{name} <{email}>"
    with project.lock():
        if version:
            names = same_version(project.config.same_version, packages[0])
            _check_version(before, names, version)
        else:
            names = sorted(packages)
        message = message or (f"Update to {version}" if version else "")
        after = _update_specs(project, before, names, version=version, author=author, message=message)
        for package in names:
            console.print(f"[bold]{package}[/]: {_evr(before[package])} -> {_evr(after[package])}")
    if version:
        console.print(f"once built, share the new archives with: xcraft push sources {' '.join(names)}")
