"""xcraft lock."""

from __future__ import annotations

from typing import Annotated

import typer

from xcraft.commands import update_build_ids
from xcraft.display import console
from xcraft.lock import choose_versions, load_lock, lock_path, save_lock
from xcraft.project import Project
from xcraft.resolution import available_versions, prepare, upstream_sources


def lock(
    packages: Annotated[
        list[str] | None,
        typer.Argument(help="Upstream packages to update, binary or source names. All by default.", show_default=False),
    ] = None,
    update: Annotated[bool, typer.Option("--update", help="Update locked packages to their latest versions.")] = False,
    outdated: Annotated[
        bool, typer.Option("--outdated", help="Show what --update would change, without changing it, and fail then.")
    ] = False,
) -> None:
    """Show the lock of upstream packages, or update it.

    --update takes the latest version all the architectures have, but the suffixes of the releases of release_suffixes
    in xcraft.toml, looking for them natively, on any machine. --outdated, for CI, fails when an update would change
    the lock.
    """
    if packages and not (update or outdated):
        msg = "packages to update need --update or --outdated"
        raise typer.BadParameter(msg)
    project = Project.find()
    if update or outdated:
        with project.lock():
            changed = _update(project, packages or [], write=update)
        if outdated:
            raise typer.Exit(1 if changed else 0)
        return
    current = load_lock(project)
    recorded = {field: getattr(current, field) for field in ("requires", "provides", "buildrequires")}
    console.print(
        f"{lock_path(project).relative_to(project.root)}: {len(current.sources)} upstream source packages, what rpm "
        f"generated for {len(recorded['requires'].shared)} binary packages of ours, the dynamic BuildRequires of "
        f"{len(recorded['buildrequires'].shared)} packages"
    )


def _update(project: Project, packages: list[str], *, write: bool) -> int:
    """Update the lock, for the `packages`, all by default, or only show what would change without `write`, and return
    the number of changes.
    """  # noqa: D205
    # first, as it may lock them
    sources = upstream_sources(project, packages) if packages else sorted(load_lock(project).sources)
    current = load_lock(project)
    chosen = choose_versions({p.arch: available_versions(p, sources) for p in project.all_platforms()})
    changed = {s: v for s, v in chosen.items() if v != current.sources[s]}
    for source, version in changed.items():
        console.print(f"[bold]{source}[/] {current.sources[source]} -> {version}")
    if not write:
        console.print(f"{len(changed)} to update: xcraft lock --update updates them" if changed else "up to date")
        return len(changed)
    if changed:
        current = load_lock(project)  # which may have locked new ones
        current.sources |= changed
        save_lock(project, current)
        prepare(project)  # the images and build roots of the updated packages
        update_build_ids(project)  # the packages built with them get new numbers
    console.print(f"{len(changed)} updated")
    return len(changed)
