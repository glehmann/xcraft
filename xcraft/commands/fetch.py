"""xcraft fetch."""

from __future__ import annotations

import typer

from xcraft import build as builder
from xcraft.commands import Packages, load, remote_storage
from xcraft.display import console
from xcraft.project import Project

app = typer.Typer()


@app.callback(invoke_without_command=True)
def everything(ctx: typer.Context) -> None:
    """Download what the caches do not have yet.

    Without a command: the current builds of all the packages, when there is a remote storage, then their sources.
    """
    if ctx.invoked_subcommand:
        return
    if Project.find().config.remote:
        builds()
    sources()


@app.command()
def sources(packages: Packages = None) -> None:
    """Put the source archives of the packages, all by default, in the source cache.

    Those missing are downloaded from the remote storage, or else from the URL of the package's Source0.
    """
    project = load(packages)
    missing = {
        name: archives
        for name in packages or sorted(project.packages)
        if (archives := builder.fetch_sources(project, name))
    }
    for name, archives in missing.items():
        console.print(f"[red]✗[/] {name}: {', '.join(archives)} found nowhere, see xcraft import {name}")
    if missing:
        raise typer.Exit(1)
    console.print("all the source archives are in the source cache")


@app.command()
def builds(packages: Packages = None) -> None:
    """Download from the remote storage the current builds of the packages, all by default, missing from the cache."""
    project = load(packages)
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
