"""xcraft clean."""

from __future__ import annotations

from typing import Annotated

import typer

from xcraft import clean as cleaner
from xcraft.cache import build_keys
from xcraft.commands import Arch, Packages, load
from xcraft.devbuilds import built_dev_suffixes
from xcraft.display import activity, console


def _size(size: int) -> str:
    return f"{size / 2**30:.1f} GiB" if size >= 2**30 else f"{size / 2**20:.0f} MiB"


def clean(
    packages: Packages = None,
    builds: Annotated[bool, typer.Option("--builds", help="Also remove the builds of the current packages.")] = False,
    ccache: Annotated[bool, typer.Option("--ccache", help="Also remove the compiler caches.")] = False,
    arch: Arch = None,
) -> None:
    """Remove the build trees and outdated builds, of the given packages only, or of all with what they share.

    The current builds are kept: the final ones, and the development ones, as last built.

    The source archives are always kept.
    """
    project = load(packages, arch=arch)
    keys = build_keys(project)
    developed = build_keys(project, built_dev_suffixes(project))  # as last built
    with project.lock(), activity("cleaning"):
        freed = cleaner.clean(project, [keys, developed], packages or None, all_builds=builds, ccache=ccache)
    console.print(f"freed {_size(freed)}")
    # the repositories xcraft repo made, which clean leaves alone, keep the files of builds
    if kept := cleaner.repos_disk_usage(project):
        console.print(f"{_size(kept)} more would be freed without the repositories of xcraft repo, in repo*/")
