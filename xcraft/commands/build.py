"""xcraft build."""

from __future__ import annotations

from collections import Counter
from typing import Annotated

import typer

from xcraft import build as builder
from xcraft.cache import cached_build
from xcraft.commands import Arch, Dev, Final, Packages, dev_ident, load, update_build_ids
from xcraft.devbuilds import dev_suffixes
from xcraft.display import console
from xcraft.graph import dependents


def build(  # noqa: PLR0913, PLR0917
    ctx: typer.Context,
    packages: Packages = None,
    no_deps: Annotated[bool, typer.Option("--no-deps", help="Do not build the dependencies.")] = False,
    with_dependents: Annotated[
        bool, typer.Option("--dependents", help="Also build the packages built against these ones.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Only show what would be built.")] = False,
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
    arch: Arch = None,
) -> None:
    """Build the given packages, all by default, and what they need, in the build environment.

    The packages whose build ID is not the main branch's get development builds, unless --final, see --dev.
    """
    ident = dev_ident(ctx, dev, final=final)
    project = load(packages, locked=locked, arch=arch)
    names = list(packages or sorted(project.packages))
    if with_dependents:
        names += sorted(dependents(project.deps, names) - set(names))
    if dry_run:
        builder.build(project, names, with_deps=not no_deps, dry_run=True, dev=ident, fetch=fetch)
        return
    with project.lock():
        update_build_ids(project)
        results, keys = builder.build(project, names, with_deps=not no_deps, verbose=verbose, dev=ident, fetch=fetch)
        update_build_ids(project)  # with what the builds found
    if autorev := dev_suffixes(project, ident):
        for r in results:
            if r.name in autorev and r.status == "built":
                for rpm in sorted((cached_build(project, r.name, keys[r.name]) / "RPMS").rglob("*.rpm")):
                    console.print(str(rpm), markup=False)
    counts = Counter(r.status for r in results)
    statuses = ("built", "fetched", "cached", "stored", "failed", "skipped")
    console.print(", ".join(f"{counts[status]} {status}" for status in statuses if counts[status]))
    if any(r.failed for r in results):
        raise typer.Exit(1)
