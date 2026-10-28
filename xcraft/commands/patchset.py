"""xcraft patchset."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from xcraft.commands import load
from xcraft.display import console
from xcraft.patchset import guess_base, guess_package, repo_name, update_patchset
from xcraft.specs import main_spec


def patchset(
    repo: Annotated[Path, typer.Argument(help="Git repository of the patchset, on its branch.")] = Path(),
    package: Annotated[
        str | None, typer.Option(help="Package of the patchset. Guessed from the name of the repository by default.")
    ] = None,
    base: Annotated[
        str | None, typer.Option(help="Base of the patchset. The tag of the version of the package by default.")
    ] = None,
    first: Annotated[int, typer.Option(help="Number of the first patch.")] = 1000,
) -> None:
    """Update the patchset of a package with the commits of a git branch, from its base.

    Run it in the repository of the patchset, on its branch.
    """
    project = load()
    packages = project.packages
    name = repo_name(repo)
    if not (package := package or guess_package(name, packages)):
        msg = f"no package matches the repository {name}, give it with --package"
        raise typer.BadParameter(msg)
    if package not in packages or not packages[package].binaries:
        msg = f"unknown package, or with a spec error: {package}"
        raise typer.BadParameter(msg)
    version = packages[package].binaries[0].evr.split(":")[-1].rsplit("-", 1)[0]
    base = base or guess_base(repo, version)
    with project.lock():
        result = update_patchset(
            project.package_dirs[package], main_spec(project.package_dirs[package]), repo, base, first
        )
    console.print(
        f"[bold]{package}[/]: patchset from {escape(base)}, {len(result.added)} added, {len(result.removed)} removed"
    )
    if result.replaced:
        console.print(f"replaced {len(result.replaced)} previous exports of the same commits:")
        for patch in result.replaced:
            console.print(f"  {patch}", markup=False)
