"""xcraft push."""

from __future__ import annotations

from typing import Annotated

import typer

from xcraft import build as builder
from xcraft import remote
from xcraft.commands import Arch, Packages, load, remote_storage
from xcraft.display import activity, console
from xcraft.project import Project
from xcraft.sources import read_manifest

app = typer.Typer()


@app.callback(invoke_without_command=True)
def everything(ctx: typer.Context, arch: Arch = None) -> None:
    """Upload to the remote storage what it does not have yet.

    Without a command: the sources and the builds of all the packages, then the pages of all its directories.
    """
    if ctx.invoked_subcommand:
        return
    sources(arch=arch)
    not_built = _push_builds(None, arch)
    index()
    if not_built:
        msg = f"not built: {', '.join(not_built)}"
        raise RuntimeError(msg)


@app.command()
def sources(
    packages: Packages = None,
    check: Annotated[
        bool, typer.Option("--check", help="Only fail when the remote storage misses archives, for CI.")
    ] = False,
    arch: Arch = None,
) -> None:
    """Upload the source archives of the packages, all by default, from the source cache."""
    project = load(packages, arch=arch)
    storage = remote_storage(project)
    archives = sorted(
        {
            (filename, digest)
            for name in packages or sorted(project.packages)
            if (manifest := project.package_dirs[name] / "sources").exists()
            for filename, digest in read_manifest(manifest).items()
        }
    )
    missing = []
    with activity("checking the remote storage") as on_line:
        for filename, digest in archives:
            on_line(filename)
            if not remote.exists(storage, builder.source_path(filename, digest)):
                missing.append((filename, digest))
    if check:
        for filename, _ in missing:
            console.print(f"[red]✗[/] {filename} is not in the remote storage")
        console.print(f"{len(archives) - len(missing)} archives in the remote storage, {len(missing)} missing")
        if missing:
            raise typer.Exit(1)
        return
    if absent := [filename for filename, digest in missing if not (project.source_cache / digest / filename).exists()]:
        msg = f"not in the source cache: {', '.join(absent)}"
        raise RuntimeError(msg)
    for filename, digest in missing:
        with activity(f"uploading {filename}"):
            remote.upload(storage, builder.source_path(filename, digest), project.source_cache / digest / filename)
        console.print(f"[green]↑[/] {filename}")
    if missing:
        with activity("making the pages of the directories"):
            remote.update_pages(storage, [f"{builder.source_path(f, d).rsplit('/', 1)[0]}/" for f, d in missing])
    console.print(f"{len(missing)} pushed, {len(archives) - len(missing)} already in the remote storage")


@app.command()
def builds(packages: Packages = None, arch: Arch = None) -> None:
    """Upload the current builds of the given packages, all by default. Test builds are never uploaded."""
    if not_built := _push_builds(packages, arch):
        msg = f"not built: {', '.join(not_built)}"
        raise RuntimeError(msg)


def _push_builds(packages: list[str] | None, arch: str | None) -> list[str]:
    """Upload the current builds of `packages`, all by default, and return those not built."""
    project = load(packages, arch=arch)
    storage = remote_storage(project)
    keys = builder.build_keys(project)
    pushed, present, not_built = [], [], []
    for name in packages or sorted(project.packages):
        build = builder.cached_build(project, name, keys[name])
        if not build.exists():
            not_built.append(name)
            continue
        path = builder.build_path(project, name, keys[name])
        if remote.tree_exists(storage, path):
            present.append(name)
            continue
        with activity(f"uploading the build of {name}"):
            remote.upload_tree(storage, path, build, builder.BUILD_FILES)
        console.print(f"[green]↑[/] {name}")
        pushed.append(name)
    if pushed:
        with activity("making the pages of the directories"):
            remote.update_pages(storage, [f"{builder.build_path(project, name, keys[name])}/" for name in pushed])
    console.print(f"{len(pushed)} pushed, {len(present)} already in the remote storage")
    return not_built


@app.command()
def index() -> None:
    """Make the index.html pages of all the directories of the remote storage, which does not list them."""
    project = Project.find()
    storage = remote_storage(project)
    with activity("making the pages of the directories"):
        count = remote.update_pages(storage, [""])
    console.print(f"{count} pages")
