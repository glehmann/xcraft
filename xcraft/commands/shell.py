"""xcraft shell."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft import rpmbuild
from xcraft.commands import Arch, complete_package, load
from xcraft.project import Project


def package_of(project: Project, given: str) -> str:
    """Return the package `given`, by name, or by a path in its directory, like ``.`` in it."""
    if given in project.package_dirs:
        return given
    path = Path(given).resolve()
    if name := next((n for n, d in project.package_dirs.items() if path.is_relative_to(d.resolve())), None):
        return name
    msg = f"{given} is neither a package nor in the directory of one"
    raise typer.BadParameter(msg)


def shell(
    package: Annotated[
        str,
        typer.Argument(
            help="Package, or a path in its directory, like . in it.",
            show_default=False,
            autocompletion=complete_package,
        ),
    ],
    arch: Arch = None,
) -> None:
    """Open a shell in the build tree of a package, where rpmbuild -ba SPECS/<spec> builds it, without the network.

    Its build dependencies are installed, and its sources prepared. The build tree is .xcraft/build/<package>/ on the
    host, where its files can be edited, and its packages are.
    """
    name = package_of(Project.find(arch), package)
    project = load([name], arch=arch)
    with project.lock():
        rpmbuild.shell(project, name)
