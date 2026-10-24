"""xcraft import."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from xcraft import process
from xcraft.commands import All, complete_package
from xcraft.display import activity, console, download_progress
from xcraft.importer import DEFAULT_URL, import_history, import_srpm
from xcraft.process import git
from xcraft.project import Project
from xcraft.resolution import prepare

if TYPE_CHECKING:
    from collections.abc import Callable


def import_(
    sources: Annotated[
        list[str] | None,
        typer.Argument(
            help="Packages, or source RPMs: their path, or URL.", show_default=False, autocompletion=complete_package
        ),
    ] = None,
    url: Annotated[
        str | None, typer.Option(help="Packaging repository of a package. Defaults to xcp-ng-rpms'.")
    ] = None,
    branch: Annotated[
        str | None, typer.Option(help="Branch to import. Defaults to the branches of [import] in xcraft.toml.")
    ] = None,
    repo: Annotated[
        str, typer.Option(help="Repository of the new packages, one of the project's. Others stay in theirs.")
    ] = "base",
    all_: All = False,
) -> None:
    """Import packages into the git branches import/<pkg>/<branch>, which the project merges: git merge <branch>.

    The history of a package's packaging repository, for the branches of [import] in xcraft.toml, rewritten for the
    project the same way each time: importing again gives the new commits on top. A source RPM, like XenServer's, as a
    new commit of the srpm_branch of [import]. --all imports again the packages which have import branches of
    histories.
    """
    project = Project.find()
    imports = project.config.import_
    if all_:
        heads = git("-C", str(project.root), "for-each-ref", "--format=%(refname:lstrip=3)", "refs/heads/import/")
        history = set(imports.branches) - {imports.srpm_branch}
        sources = sorted(
            {pkg for pkg, _, branch in (head.partition("/") for head in heads.splitlines()) if branch in history}
        )
    if not sources:
        msg = "no package given"
        raise typer.BadParameter(msg)
    names = _names(project, sources, repo, url)
    branches = [branch] if branch else imports.branches
    with project.lock():
        if any(source.endswith(".src.rpm") for source in sources):
            prepare(project, roots=False)  # for the build environment, which unpacks them
        for i, (source, name) in enumerate(zip(sources, names, strict=True), 1):
            pkg_dir = project.package_dirs.get(name, project.root / repo / name)
            with project.work("import") as work, activity(f"[{i}/{len(sources)}] importing {source}") as on_line:
                if not source.endswith(".src.rpm"):
                    heads = import_history(project.root, pkg_dir, url or DEFAULT_URL.format(name=name), branches)
                elif commit := import_srpm(project, _download(source, work, on_line), pkg_dir, on_line=on_line):
                    heads = {imports.srpm_branch: commit}
                else:
                    heads = {}
                    console.print(
                        f"[bold]{name}[/]: import/{name}/{imports.srpm_branch} has the content of {source} already"
                    )
            for head, commit in heads.items():
                console.print(f"[bold]{name}[/]: import/{name}/{head} at {commit[:12]}")
    console.print("merge them into the project with [bold]git merge import/<pkg>/<branch>[/]")


def _names(project: Project, sources: list[str], repo: str, url: str | None) -> list[str]:
    """Return the names of the packages of `sources`, checking them with the other arguments."""
    names = [
        s.rsplit("/", 1)[-1].removesuffix(".src.rpm").rsplit("-", 2)[0] if s.endswith(".src.rpm") else s
        for s in sources
    ]
    if bad := [name for name in [*names, repo] if not re.fullmatch(r"[A-Za-z0-9][\w.+-]*", name)]:
        msg = f"not package names: {', '.join(map(repr, bad))}"
        raise typer.BadParameter(msg)
    if repo not in project.config.repositories:
        msg = f"{repo} is not a repository of the project: {', '.join(project.config.repositories)}"
        raise typer.BadParameter(msg)
    if url and len(sources) > 1:
        msg = "--url applies to a single package"
        raise typer.BadParameter(msg)
    return names


def _download(source: str, work: Path, on_line: Callable[[str], None]) -> Path:
    """Return the path of the source RPM `source`, downloaded into `work` from a URL."""
    if "://" not in source:
        return Path(source).absolute()
    path = work / source.rsplit("/", 1)[-1]
    process.download(source, path, download_progress(on_line, f"downloading {path.name}: "))
    return path
