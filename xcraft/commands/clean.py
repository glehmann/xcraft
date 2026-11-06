"""xcraft clean."""

from __future__ import annotations

import math
import os
import time
from typing import Annotated

import typer

from xcraft import clean as cleaner
from xcraft.cache import build_keys
from xcraft.commands import Arch, Packages, prepared_project
from xcraft.devbuilds import built_dev_suffixes
from xcraft.display import activity, console
from xcraft.iso import checkout_iso_keys


def _size(size: int) -> str:
    return f"{size / 2**30:.1f} GiB" if size >= 2**30 else f"{size / 2**20:.0f} MiB"


def clean(
    packages: Packages = None,
    builds: Annotated[bool, typer.Option("--builds", help="Also remove the builds of the current packages.")] = False,
    ccache: Annotated[bool, typer.Option("--ccache", help="Also remove the compiler caches.")] = False,
    dune_cache: Annotated[bool, typer.Option("--dune-cache", help="Also remove the caches of dune.")] = False,
    sources: Annotated[
        bool,
        typer.Option(
            "--sources",
            help="Also remove the source archives no package of the checkout has, like those of other branches: push "
            "them first, see xcraft push sources, unless the remote storage or the URLs of their specs have them.",
        ),
    ] = False,
    unused: Annotated[
        float | None,
        typer.Option(
            "--unused",
            metavar="DAYS",
            help="Keep the outdated builds, ISOs, specs, resolutions and source archives used in the last DAYS days, "
            "like those of other branches.",
            show_default=False,
        ),
    ] = None,
    arch: Arch = None,
) -> None:
    """Remove the build trees and outdated builds, signed or not, of the given packages only, or of all with what they
    share: the outdated caches of the specs, the resolutions and the checks, the upstream packages downloaded, and the
    logs of the packages removed from the project. What is of an architecture is of the one of --arch only.

    The current builds are kept: the final ones, and the development ones, as last built. So are the ISOs of the
    checkout, see xcraft iso, but the others, from the remote storage or made again when needed.

    The source archives are kept, but those no package has with --sources.
    """
    if sources and packages:
        msg = "--sources removes the archives of no package: without packages"
        raise typer.BadParameter(msg)
    project = prepared_project(packages, arch=arch)
    keys = build_keys(project)
    developed = build_keys(project, built_dev_suffixes(project))  # as last built
    isos = checkout_iso_keys(project, os.environ.get("XCRAFT_DEV") or "dev")
    with project.sole_use(), activity("cleaning"):
        freed = cleaner.clean(
            project,
            [keys, developed],
            packages or None,
            all_builds=builds,
            ccache=ccache,
            dune_cache=dune_cache,
            sources=sources,
            used_since=math.inf if unused is None else time.time() - unused * 86400,
            isos=isos,
        )
    console.print(f"freed {_size(freed)}")
    # the repositories xcraft repo made, which clean leaves alone, keep the files of builds
    if kept := cleaner.repos_disk_usage(project):
        console.print(f"{_size(kept)} more would be freed without the repositories of xcraft repo, in repo*/")
