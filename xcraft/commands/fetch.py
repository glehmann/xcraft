"""xcraft fetch."""

from __future__ import annotations

import typer

from xcraft import build as builder
from xcraft import ccache
from xcraft.cache import build_keys, cached_build
from xcraft.commands import Arch, Final, Packages, load
from xcraft.devbuilds import developed
from xcraft.display import console
from xcraft.project import Project
from xcraft.sources import SourceError, fetch_sources

app = typer.Typer()


@app.callback(invoke_without_command=True)
def everything(ctx: typer.Context, arch: Arch = None) -> None:
    """Download what the caches do not have yet.

    Without a command: the current builds of all the packages, when there is a remote storage, then their sources.
    """
    if ctx.invoked_subcommand:
        # all the options given before the subcommand are its defaults: those given after it win
        ctx.default_map = {ctx.invoked_subcommand: {k: v for k, v in ctx.params.items() if v is not None}}
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
            if archives := fetch_sources(project, name):
                missing[name] = (
                    f"{', '.join(archives)} found nowhere: not in the remote storage, nor at a URL of the spec"
                )
        except SourceError as e:
            missing[name] = str(e)
    for name, why in missing.items():
        console.print(f"[red]✗[/] {name}: {why}")
    if missing:
        raise typer.Exit(1)
    console.print("all the source archives are in the source cache")


@app.command("ccache")
def caches(packages: Packages = None, arch: Arch = None) -> None:
    """Add to the local compiler caches of the given packages, all by default, the entries of the remote storage.

    They are not downloaded by xcraft fetch without a command, but a build gets the one of its package when it has
    none.
    """
    project = load(packages, arch=arch)
    if not project.config.remote:
        msg = "no remote storage: see [remote.main] and [remote.dev] in xcraft.toml"
        raise typer.BadParameter(msg)
    fetched = [name for name in packages or sorted(project.packages) if ccache.fetch(project, name)]
    for name in fetched:
        console.print(f"[green]↓[/] {name}")
    console.print(f"{len(fetched)} fetched")


@app.command()
def builds(packages: Packages = None, final: Final = False, arch: Arch = None) -> None:
    """Download from the remote storage the current builds of the packages, all by default, missing from the cache.

    The packages with development builds are skipped, unless --final, which downloads their final builds, if any.
    """
    project = load(packages, arch=arch)
    if not project.config.remote:
        msg = "no remote storage: see [remote.main] and [remote.dev] in xcraft.toml"
        raise typer.BadParameter(msg)
    keys, skipped = build_keys(project), set() if final else developed(project)
    if shown := sorted(skipped & {*(packages or project.packages)}):
        console.print(f"development builds, not fetched: {', '.join(shown)}")
    fetched, cached, absent = [], [], []
    for name in [name for name in packages or sorted(project.packages) if name not in skipped]:
        if cached_build(project, name, keys[name]).exists():
            cached.append(name)
        elif builder.fetch_build(project, name, keys[name]):
            fetched.append(name)
            console.print(f"[green]↓[/] {name}")
        else:
            absent.append(name)
    console.print(f"{len(fetched)} fetched, {len(cached)} already cached, {len(absent)} not in the remote storage")
