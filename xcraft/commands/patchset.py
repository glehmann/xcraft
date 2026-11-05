"""xcraft patchset."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from xcraft import specs
from xcraft.display import console
from xcraft.patchset import guess_base, guess_package, repo_name, update_patchset
from xcraft.project import Project
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
    project = Project.find()
    dirs = project.package_dirs
    name = repo_name(repo)
    # the specs parsed only when the name of the repository is not the one of a package directory
    if not (package := package or (name if name in dirs else guess_package(name, project.parsed))):
        msg = f"no package matches the repository {name}, give it with --package"
        raise typer.BadParameter(msg)
    if package not in dirs:
        msg = f"unknown package: {package}"
        raise typer.BadParameter(msg)
    if not base:
        if not (binaries := specs.parse(project, [dirs[package]])[package].binaries):
            msg = f"the spec of {package} cannot be parsed: give the base of the patchset with --base"
            raise typer.BadParameter(msg)
        base = guess_base(repo, binaries[0].evr.split(":")[-1].rsplit("-", 1)[0])
    # like any edit of the package: the builds hash what they stage
    result = update_patchset(dirs[package], main_spec(dirs[package]), repo, base, first)
    console.print(
        f"[bold]{package}[/]: patchset from {escape(base)}, {len(result.added)} added, {len(result.removed)} removed"
    )
    if result.replaced:
        console.print(f"replaced {len(result.replaced)} previous exports of the same commits:")
        for patch in result.replaced:
            console.print(f"  {patch}", markup=False)
