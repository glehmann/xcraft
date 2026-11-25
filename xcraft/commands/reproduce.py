"""xcraft reproduce."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from xcraft import builders
from xcraft.commands import (
    Arch,
    Dev,
    Final,
    Key,
    NoCheck,
    Packages,
    dev_ident,
    prepared_project,
    sign_builds,
    unprepared_project,
)
from xcraft.display import console
from xcraft.reproduce import DIR
from xcraft.reproduce import reproduce as reproduced


def reproduce(
    ctx: typer.Context,
    packages: Packages = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show the build logs.")] = False,
    locked: Annotated[bool, typer.Option("--locked", help="Fail when building would change the lock, for CI.")] = False,
    dev: Dev = None,
    final: Final = False,
    nocheck: NoCheck = False,
    with_caches: Annotated[
        bool,
        typer.Option(
            "--with-caches",
            help="Keep the compiler caches, for a quick check of what they do not cache, like the links and the "
            "packaging: what they cache is the same by construction.",
        ),
    ] = False,
    arch: Arch = None,
    key: Key = None,
    local: Annotated[
        bool,
        typer.Option("--local", help="Build on this machine, rather than on the builder of the architecture, if any."),
    ] = False,
) -> None:
    """Build the given packages again, all by default, and check that they are the same as their builds: reproducible.

    Each one is built again alone, against the same builds of the packages it is built against, without its compiler
    caches, unless --with-caches, nor anything fetched, with the same key and release, then compared with its build: the
    same packages, files, by their paths, digests, modes, owners, groups, flags and link targets, not their times,
    dependencies and scriptlets. Its build stays; the rebuild is dropped, or kept in .xcraft/reproduce/<package>/again
    when it differs, which fails. Their builds, and those of what they need, are made or fetched first.

    On the builder of the architecture, if any, unless --local, like xcraft build.
    """
    ident = dev_ident(ctx, dev, final=final, nocheck=nocheck)
    if not local and (host := builders.builders().get((project := unprepared_project(packages, arch=arch)).arch)):
        flags = [*(["--verbose"] if verbose else []), *(["--locked"] if locked else [])]
        flags += [*(["--nocheck"] if nocheck else []), *(["--dev", ident] if ident else ["--final"])]
        flags += ["--with-caches"] if with_caches else []
        code = builders.reproduce(project, host, [*(packages or []), *flags, "--arch", project.arch, "--local"])
        raise typer.Exit(code)
    project = prepared_project(packages, locked=locked, arch=arch)
    sign_builds(project, key, final=ident is None)
    with project.lock():
        found = reproduced(
            project,
            list(packages or sorted(project.packages)),
            dev=ident,
            nocheck=nocheck,
            verbose=verbose,
            with_caches=with_caches,
        )
    for name, differences in found.items():
        if not differences:
            console.print(f"[bold green]✓[/] {name}: built again the same")
            continue
        console.print(f"[bold red]✗[/] {name}: built again differently, see .xcraft/{DIR}/{name}/again")
        for difference in differences[:20]:
            console.print(f"  {escape(difference)}")
        if len(differences) > 20:  # noqa: PLR2004
            console.print(f"  and {len(differences) - 20} more")
    same = sum(not d for d in found.values())
    console.print(f"{same} reproducible, {len(found) - same} not")
    if same < len(found):
        raise typer.Exit(1)
