"""xcraft clean."""

from __future__ import annotations

import math
import os
import time
from contextlib import nullcontext
from typing import Annotated

import typer

from xcraft import clean as cleaner
from xcraft import pinned
from xcraft.cache import build_keys
from xcraft.commands import Arch, Packages, prepared_project
from xcraft.container import checkout_images, checkout_repositories
from xcraft.devbuilds import built_dev_suffixes
from xcraft.display import activity, console
from xcraft.iso import checkout_iso_keys
from xcraft.project import Project

_ALSO, _RETENTION = "Additional cleanup", "Retention"


def _size(size: int) -> str:
    return f"{size / 2**30:.1f} GiB" if size >= 2**30 else f"{size / 2**20:.0f} MiB"


def _shared(project: Project, used_since: float, *, dry_run: bool) -> dict[str, list[str]]:
    """Return the images and the repositories of pinned packages of the machine to remove, removed unless `dry_run`."""
    images = cleaner.other_images(checkout_images(project), used_since)
    repos = pinned.other_repositories(checkout_repositories(project), used_since)
    if not dry_run:
        images = cleaner.remove_images(images)
        pinned.remove_repositories(repos)
    return {what: names for what, names in {"image tags": images, "pinned repositories": repos}.items() if names}


def clean(
    packages: Packages = None,
    current_builds: Annotated[
        bool,
        typer.Option(
            "--current-builds",
            help="Also remove the current builds. --keep-days still keeps the outdated ones used recently.",
            rich_help_panel=_ALSO,
        ),
    ] = False,
    ccache: Annotated[
        bool, typer.Option("--ccache", help="Also remove the compiler caches.", rich_help_panel=_ALSO)
    ] = False,
    dune_cache: Annotated[
        bool, typer.Option("--dune-cache", help="Also remove the caches of dune.", rich_help_panel=_ALSO)
    ] = False,
    unused_sources: Annotated[
        bool,
        typer.Option(
            "--unused-sources",
            help="Also remove the source archives no package of this checkout references. Applies to the whole "
            "checkout, even when packages are given. Archives of other branches may exist only here: push them first, "
            "see xcraft push sources.",
            rich_help_panel=_ALSO,
        ),
    ] = False,
    shared: Annotated[
        bool,
        typer.Option(
            "--shared",
            help="Also remove the machine-wide build environment images and pinned-package repositories this checkout "
            "does not need. Other checkouts make theirs again when needed, and their running commands may fail.",
            rich_help_panel=_ALSO,
        ),
    ] = False,
    keep_days: Annotated[
        float | None,
        typer.Option(
            "--keep-days",
            metavar="DAYS",
            min=0,
            help="Keep the outdated builds, ISOs, metadata and source archives used in the last DAYS days, like those "
            "of other branches. With --shared, also keep the images built, and the pinned-package repositories used, "
            "in the last DAYS days. The rest is removed whatever its age.",
            show_default=False,
            rich_help_panel=_RETENTION,
        ),
    ] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Only show what would be removed.")] = False,
    arch: Arch = None,
) -> None:
    """Remove build trees and outdated cached data.

    Package names limit the cleanup to their build trees and outdated builds. Without package names, also clean the
    temporary files, outdated ISOs and metadata, results of checks, downloaded upstream packages, and logs of removed
    packages. Builds, ISOs and metadata are those of the --arch architecture only.

    Keep the current final and last-built development builds, the ISOs of the checkout, the source archives and the
    compiler caches, unless additional cleanup is requested.
    """
    project = prepared_project(packages, arch=arch)
    names = packages or None
    keys = [build_keys(project), build_keys(project, built_dev_suffixes(project))]  # the development ones as last built
    isos = checkout_iso_keys(project, os.environ.get("XCRAFT_DEV") or "dev")
    used_since = math.inf if keep_days is None else time.time() - keep_days * 86400
    with nullcontext() if dry_run else project.sole_use(), activity("cleaning"):
        removal = cleaner.routine_removal(
            project, keys, names, isos=isos, current=current_builds, used_since=used_since
        )
        for cache, wanted in (("ccache", ccache), ("dune-cache", dune_cache)):
            removal |= cleaner.caches_removal(project, cache, names) if wanted else {}
        removal |= cleaner.sources_removal(project, used_since) if unused_sources else {}
        size = cleaner.freed([p for paths in removal.values() for p in paths])
        if not dry_run:
            cleaner.remove(project, removal)
        removed = {**removal, **(_shared(project, used_since, dry_run=dry_run) if shared else {})}
    width = max((len(what) for what in removed), default=0)
    for what, items in removed.items():
        console.print(f"{what:<{width}} {len(items):>6} {'to remove' if dry_run else 'removed'}")
    console.print(f"{'would free' if dry_run else 'freed'} {_size(size)} in .xcraft/")
    # the repositories xcraft repo made, which clean leaves alone, keep the files of builds
    if kept := cleaner.repos_disk_usage(project):
        console.print(f"{_size(kept)} more would be freed without the repositories of xcraft repo, in repo*/")
