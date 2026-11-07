"""xcraft build."""

from __future__ import annotations

from collections import Counter
from typing import Annotated

import typer

from xcraft import build as builder
from xcraft.cache import cached_build
from xcraft.commands import Arch, Dev, Final, Key, NoCheck, Packages, dev_ident, prepared_project, sign_builds
from xcraft.display import console
from xcraft.graph import dependents


def _update_ids(packages: list[str] | None, *, dry_run: bool, locked: bool, arch: str | None) -> None:
    """Update the build IDs of all the packages, for xcraft build --ids, or check them with `locked`."""
    if packages or dry_run:
        msg = "--ids updates the build IDs of all the packages: without packages, nor --dry-run"
        raise typer.BadParameter(msg)
    project = prepared_project(locked=locked, arch=arch)
    with project.lock():
        if not builder.update_build_ids(project):
            console.print("the build IDs are up to date")


def build(
    ctx: typer.Context,
    packages: Packages = None,
    no_deps: Annotated[bool, typer.Option("--no-deps", help="Do not build the dependencies.")] = False,
    with_dependents: Annotated[
        bool, typer.Option("--dependents", help="Also build the packages built against these ones.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Only show what would be built.")] = False,
    ids: Annotated[
        bool,
        typer.Option(
            "--ids",
            help="Only update the build IDs of all the packages in xcraft.builds, as the builds would, without "
            "building: or check them with --locked.",
        ),
    ] = False,
    fetch: Annotated[
        builder.Fetch,
        typer.Option(
            help="What not cached locally is fetched from the remote storage, rather than built: all it has, only the "
            "builds the packages to build need, like in CI, or none.",
        ),
    ] = "all",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show the build logs.")] = False,
    locked: Annotated[
        bool, typer.Option("--locked", help="Fail the builds that would change the lock, for CI.")
    ] = False,
    dev: Dev = None,
    final: Final = False,
    nocheck: NoCheck = False,
    arch: Arch = None,
    key: Key = None,
) -> None:
    r"""Build the given packages, all by default, and what they need, in the build environment.

    The packages whose build ID is not the main branch's get development builds, unless --final, see --dev. The builds
    are signed by --key, or the default key: \[sign], or else the developer's, which xcraft makes for the development
    builds when there is none, but not for --final.
    """
    ident = dev_ident(ctx, dev, final=final, nocheck=nocheck)
    if ids:
        _update_ids(packages, dry_run=dry_run, locked=locked, arch=arch)
        return
    project = prepared_project(packages, locked=locked, arch=arch)
    names = list(packages or sorted(project.packages))
    if with_dependents:
        names += sorted(dependents(project.deps, names) - set(names))
    if dry_run:
        builder.build(project, names, with_deps=not no_deps, dry_run=True, dev=ident, fetch=fetch)
        return
    sign_builds(project, key, final=ident is None)
    with project.lock():
        results, keys, autorev = builder.build(
            project, names, with_deps=not no_deps, verbose=verbose, dev=ident, nocheck=nocheck, fetch=fetch
        )
    if dev_built := [r.name for r in results if r.name in autorev and r.status == "built"]:
        if verbose:
            for name in dev_built:
                for rpm in sorted((cached_build(project, name, keys[name]) / "RPMS").rglob("*.rpm")):
                    console.print(str(rpm), markup=False)
        console.print(
            f"development builds: xcraft repo {' '.join(dev_built)} makes a repository of them, for the hosts"
        )
    counts = Counter(r.status for r in results)
    statuses = ("built", "fetched", "cached", "stored", "failed", "skipped")
    console.print(", ".join(f"{counts[status]} {status}" for status in statuses if counts[status]))
    if any(r.failed for r in results):
        raise typer.Exit(1)
