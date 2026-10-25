"""xcraft build."""

from __future__ import annotations

from collections import Counter
from typing import Annotated

import typer

from xcraft import build as builder
from xcraft.cache import cached_build
from xcraft.commands import Arch, Packages, Pre, Test, load, test_kind
from xcraft.display import console
from xcraft.graph import dependents
from xcraft.testbuilds import next_test_suffixes


def build(  # noqa: PLR0913, PLR0917
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
    test: Test = None,
    pre: Pre = None,
    arch: Arch = None,
) -> None:
    """Build the given packages, all by default, and what they need, in the build environment.

    Each test or pre build of a package gets the next number <n> for its ID and release.
    """
    kind = test_kind(test, pre, packages)
    project = load(packages, locked=locked, arch=arch)
    names = list(packages or sorted(project.packages))
    if with_dependents:
        names += sorted(dependents(project.deps, names) - set(names))
    if dry_run:
        autorev = dict.fromkeys(names, "?") if kind else None
        builder.build(project, names, with_deps=not no_deps, dry_run=True, autorev=autorev, fetch=fetch)
        return
    with project.lock():
        autorev = next_test_suffixes(project, names, *kind) if kind else {}
        results, keys = builder.build(
            project, names, with_deps=not no_deps, verbose=verbose, autorev=autorev, fetch=fetch
        )
    if autorev:
        for r in results:
            if r.name in autorev and r.status == "built":
                for rpm in sorted((cached_build(project, r.name, keys[r.name]) / "RPMS").rglob("*.rpm")):
                    console.print(str(rpm), markup=False)
    counts = Counter(r.status for r in results)
    statuses = ("built", "fetched", "cached", "stored", "failed", "skipped")
    console.print(", ".join(f"{counts[status]} {status}" for status in statuses if counts[status]))
    if any(r.failed for r in results):
        raise typer.Exit(1)
