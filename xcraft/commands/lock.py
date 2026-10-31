"""xcraft lock."""

from __future__ import annotations

from typing import Annotated

import typer

from xcraft.build import update_build_ids
from xcraft.display import console
from xcraft.lock import choose_versions, load_lock, lock_path, save_lock, upgrade_lock
from xcraft.project import Project
from xcraft.resolution import available_versions, prepare, upstream_sources


def lock(
    packages: Annotated[
        list[str] | None,
        typer.Argument(help="Upstream packages to update, binary or source names. All by default.", show_default=False),
    ] = None,
    update: Annotated[bool, typer.Option("--update", help="Update locked packages to their latest versions.")] = False,
    outdated: Annotated[
        bool,
        typer.Option(
            "--outdated", help="Show the versions --update would change, without changing them, and fail then."
        ),
    ] = False,
    upgrade: Annotated[
        bool, typer.Option("--upgrade", help="Upgrade a lock of an older xcraft, pinning its packages again.")
    ] = False,
) -> None:
    """Show the lock of upstream packages, or update it.

    --update takes the latest version all the architectures have, but the suffixes of the releases of release_suffixes
    in xcraft.toml, looking for them natively, on any machine, and pins their binary packages again, by their SHA-256,
    even in the same versions, like those published again. Otherwise, the pinned packages are kept, whatever the
    repositories have now: a build fails rather than getting other ones. --outdated, for CI, fails when an update
    would change the versions in the lock: not when it would only pin other contents of the same versions, which only
    --update looks for.
    """
    if packages and not (update or outdated):
        msg = "packages to update need --update or --outdated"
        raise typer.BadParameter(msg)
    project = Project.find()
    if upgrade:
        with project.lock():
            save_lock(project, upgrade_lock(project))
            prepare(project)
            update_build_ids(project)
        console.print(f"{lock_path(project).relative_to(project.root)} upgraded")
        return
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
    """
    # first, as it may lock them
    sources = upstream_sources(project, packages) if packages else sorted(load_lock(project).sources)
    current = load_lock(project)
    chosen = choose_versions({p.arch: available_versions(p, sources) for p in project.all_platforms()})
    changed = {s: v for s, v in chosen.items() if v != current.sources[s]}
    for source, version in changed.items():
        console.print(f"[bold]{source}[/] {current.sources[source]} -> {version}")
    if not write:
        # not what other contents of the same versions the repositories may have, which only --update pins
        up_to_date = (
            "versions up to date, not looking for other contents of the same versions: xcraft lock --update does"
        )
        console.print(f"{len(changed)} to update: xcraft lock --update updates them" if changed else up_to_date)
        return len(changed)
    current = load_lock(project)  # which may have locked new ones
    current.sources |= changed
    save_lock(project, current)
    prepare(project, repin=set(sources))  # the images and build roots of the updated packages, pinned again
    update_build_ids(project)  # the packages built with them get new numbers
    pins = {arch: dict(pins) for arch, pins in current.binaries.items()}
    repinned = sorted(
        f"{name} ({arch})"
        for arch, now in load_lock(project).binaries.items()
        for name, pin in now.items()
        if pin.source not in changed and pins.get(arch, {}).get(name, pin) != pin
    )
    if repinned:
        console.print(f"pinned again, of the same versions: {', '.join(repinned)}")
    console.print(f"{len(changed)} updated")
    return len(changed) + len(repinned)
