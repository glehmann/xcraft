"""xcraft develop."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from xcraft import develop as developer
from xcraft.display import console
from xcraft.patchset import guess_package, repo_name
from xcraft.process import git
from xcraft.project import Project


def _package(project: Project, tree: Path, package: str | None) -> str:
    """Return the package developed from `tree`: `package`, or the one named after its repository."""
    name = repo_name(tree)
    if not (package := package or (name if name in project.package_dirs else guess_package(name, project.parsed))):
        msg = f"no package matches the repository {name}, give it with --package"
        raise typer.BadParameter(msg)
    if package not in project.package_dirs:
        msg = f"unknown package: {package}"
        raise typer.BadParameter(msg)
    return package


def develop(
    trees: Annotated[
        list[Path] | None,
        typer.Argument(help="Git trees of the projects of packages, or with --stop, packages.", show_default=False),
    ] = None,
    package: Annotated[
        str | None,
        typer.Option(
            help="Package of the tree. Guessed from the name of its repository by default.", show_default=False
        ),
    ] = None,
    stop: Annotated[bool, typer.Option("--stop", help="Stop developing the given packages, all by default.")] = False,
) -> None:
    """Build packages from the git trees of their projects, as they are, or show those developed from trees.

    Their development builds take the files of the tree, committed or not, but those git ignores, rather than their
    main archive and patches, and keep the directory of their sources from one build to the next, for incremental
    builds. Their versions are those of their specs. Until --stop, for this checkout only: the final builds, publish
    and push refuse them. See docs/builds.md.
    """
    project = Project.find()
    developed = developer.trees(project)
    if stop:
        if unknown := sorted({str(n) for n in trees or []} - developed.keys()):
            msg = f"not developed from a tree: {', '.join(unknown)}"
            raise typer.BadParameter(msg)
        stopped = [str(n) for n in trees] if trees else list(developed)
        developer.save(project, {n: t for n, t in developed.items() if n not in stopped})
        console.print(f"stopped developing {', '.join(stopped)}" if stopped else "no package developed from a tree")
        return
    if package and len(trees or []) != 1:
        msg = "--package names the package of one tree"
        raise typer.BadParameter(msg)
    for tree in trees or []:
        try:
            root = Path(git("-C", str(tree), "rev-parse", "--show-toplevel"))
        except subprocess.CalledProcessError as e:
            msg = f"{tree} is not a git tree"
            raise typer.BadParameter(msg) from e
        name = _package(project, root, package)
        developed[name] = root
        developer.save(project, developed)
        console.print(f"[bold]{name}[/]: developed from {escape(str(root))}")
    if trees:
        return
    for name, path in developed.items():
        state = developer.tree(project, name)
        console.print(f"[bold]{name}[/] ← {escape(str(path))}" + (f" {escape(state.describe)}" if state else ""))
    if not developed:
        console.print("no package developed from a tree")
