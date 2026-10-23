"""xcraft import."""

from __future__ import annotations

import re
from typing import Annotated

import typer

from xcraft.commands import All, Packages
from xcraft.display import activity, console
from xcraft.importer import DEFAULT_URL, import_package, read_origin
from xcraft.project import Project


def import_(
    packages: Packages = None,
    branch: Annotated[
        str | None, typer.Option(help="Branch to import. Defaults to the previous import's, or master.")
    ] = None,
    url: Annotated[
        str | None,
        typer.Option(help="Packaging repository, or source RPM. Defaults to the previous import's, or xcp-ng-rpms'."),
    ] = None,
    repo: Annotated[
        str, typer.Option(help="Repository of the new packages, one of the project's. Others stay in theirs.")
    ] = "base",
    all_: All = False,
) -> None:
    """Copy packages from their packaging repositories into <repo>/.

    --all re-imports the packages imported before.
    """
    project = Project.find()
    if all_:
        packages = sorted(name for name, path in project.package_dirs.items() if (path / "origin").exists())
    if not packages:
        msg = "no package given"
        raise typer.BadParameter(msg)
    if bad := [name for name in [*packages, repo] if not re.fullmatch(r"[A-Za-z0-9][\w.+-]*", name)]:
        msg = f"not package names: {', '.join(map(repr, bad))}"
        raise typer.BadParameter(msg)
    if repo not in project.config.repositories:
        msg = f"{repo} is not a repository of the project: {', '.join(project.config.repositories)}"
        raise typer.BadParameter(msg)
    if url and len(packages) > 1:
        msg = "--url applies to a single package"
        raise typer.BadParameter(msg)
    with project.lock():
        for i, name in enumerate(packages, 1):
            pkg_dir = project.package_dirs.get(name, project.root / repo / name)
            origin_url, origin_branch, _ = read_origin(pkg_dir) or (DEFAULT_URL.format(name=name), "master", "")
            # the previous branch, of the previous repository only
            pkg_branch = branch or (origin_branch if not url or url == origin_url else "master")
            with activity(f"[{i}/{len(packages)}] importing {name}") as on_line:
                commit = import_package(project, pkg_dir, url or origin_url, pkg_branch, on_line=on_line)
            source = "the source RPM" if (url or origin_url).endswith(".src.rpm") else pkg_branch
            console.print(f"[bold]{name}[/]: imported {source} at {commit[:12]}")
