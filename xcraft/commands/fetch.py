"""xcraft fetch."""

from __future__ import annotations

import typer

from xcraft import build as builder
from xcraft import ccache
from xcraft.commands import Arch, Packages, load, remote_storage
from xcraft.display import console
from xcraft.project import Project

app = typer.Typer()


@app.callback(invoke_without_command=True)
def everything(ctx: typer.Context, arch: Arch = None) -> None:
    """Download what the caches do not have yet.

    Without a command: the current builds of all the packages, when there is a remote storage, then their sources.
    """
    if ctx.invoked_subcommand:
        return
    if Project.find(arch).config.remote:
        builds(arch=arch)
    sources(arch=arch)


@app.command()
def sources(packages: Packages = None, arch: Arch = None) -> None:
    """Put the source archives of the packages, all by default, in the source cache.

    Those missing are downloaded from the remote storage, or else from the source of the spec with their URL.
    """
    project = load(packages, arch=arch)
    missing = {}
    for name in packages or sorted(project.packages):
        try:  # the others too, when a download fails
            if archives := builder.fetch_sources(project, name):
                missing[name] = (
                    f"{', '.join(archives)} found nowhere: not in the remote storage, nor at a URL of the spec"
                )
        except builder.BuildError as e:
            missing[name] = str(e)
    for name, why in missing.items():
        console.print(f"[red]✗[/] {name}: {why}")
    if missing:
        raise typer.Exit(1)
    console.print("all the source archives are in the source cache")


@app.command("ccache")
def caches(packages: Packages = None, arch: Arch = None) -> None:
    """Download the compiler caches of the given packages, all by default, replacing the local ones.

    They are not downloaded by xcraft fetch without a command, but a build gets the one of its package when it has
    none.
    """
    project = load(packages, arch=arch)
    storage = remote_storage(project)
    fetched = [name for name in packages or sorted(project.packages) if ccache.fetch(project, storage, name)]
    for name in fetched:
        console.print(f"[green]↓[/] {name}")
    console.print(f"{len(fetched)} fetched")


@app.command()
def builds(packages: Packages = None, arch: Arch = None) -> None:
    """Download from the remote storage the current builds of the packages, all by default, missing from the cache."""
    project = load(packages, arch=arch)
    remote_storage(project)
    keys = builder.build_keys(project)
    fetched, cached, absent = [], [], []
    for name in packages or sorted(project.packages):
        if builder.cached_build(project, name, keys[name]).exists():
            cached.append(name)
        elif builder.fetch_build(project, name, keys[name]):
            fetched.append(name)
            console.print(f"[green]↓[/] {name}")
        else:
            absent.append(name)
    console.print(f"{len(fetched)} fetched, {len(cached)} already cached, {len(absent)} not in the remote storage")
