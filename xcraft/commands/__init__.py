"""The xcraft subcommands, one per module, and what they share."""

from __future__ import annotations

import re
from typing import Annotated

import typer

from xcraft.project import Project, Remote


def complete_package(incomplete: str) -> list[str]:
    """Return the packages starting with `incomplete`, for the shell completion."""
    try:
        return sorted(name for name in Project.find().package_dirs if name.startswith(incomplete))
    except (RuntimeError, OSError):
        return []


Packages = Annotated[
    list[str] | None, typer.Argument(help="Packages.", show_default=False, autocompletion=complete_package)
]
OnePackage = Annotated[str, typer.Argument(help="Package.", show_default=False, autocompletion=complete_package)]
All = Annotated[bool, typer.Option("--all", help="All packages.")]


def load(names: list[str] | None = None, *, locked: bool = False) -> Project:
    """Return the project, with its packages and their build dependencies, checking `names` are packages.

    With `locked`, the lock cannot change, even to build the build environment image.
    """
    project = Project.find()
    project.frozen = locked
    if unknown := sorted(set(names or []) - project.packages.keys()):
        msg = f"unknown package(s): {', '.join(unknown)}"
        raise typer.BadParameter(msg)
    return project


def remote_storage(project: Project) -> Remote:
    """Return the remote storage of the project, which the command needs."""
    if not (storage := project.config.remote):
        msg = "no remote storage: see [remote] in xcraft.toml"
        raise typer.BadParameter(msg)
    return storage


Test = Annotated[
    str | None,
    typer.Option(
        metavar="ID",
        help="Test builds of the given packages, above their release and below the next one: <release>.0.ID.<n>.",
    ),
]
Pre = Annotated[
    str | None,
    typer.Option(metavar="ID", help="Pre builds of the given packages, below their release: <release>~ID.<n>."),
]


def test_kind(test: str | None, pre: str | None, packages: list[str] | None) -> tuple[str, str] | None:
    """Return the kind and ID of the test builds of `packages` asked with --test or --pre, if any."""
    if test and pre:
        msg = "--test and --pre cannot be used together"
        raise typer.BadParameter(msg)
    if (test or pre) and not packages:
        msg = "test and pre builds are of the given packages only"
        raise typer.BadParameter(msg)
    if not (ident := test or pre):
        return None
    if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", ident):
        msg = f"the build ID must be 1 to 16 letters and digits: {ident}"
        raise typer.BadParameter(msg)
    return ("test" if test else "pre"), ident
