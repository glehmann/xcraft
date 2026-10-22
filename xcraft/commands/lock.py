"""xcraft lock."""

from __future__ import annotations

from typing import Annotated

import typer

from xcraft.commands import Arch
from xcraft.container import latest_versions
from xcraft.display import console
from xcraft.lock import load_lock, lock_path, save_lock
from xcraft.project import Project


def lock(
    packages: Annotated[
        list[str] | None,
        typer.Argument(help="Upstream packages to update, binary or source names. All by default.", show_default=False),
    ] = None,
    update: Annotated[bool, typer.Option("--update", help="Update locked packages to their latest versions.")] = False,
    arch: Arch = None,
) -> None:
    """Show the lock of upstream packages, or update it."""
    if packages and not update:
        msg = "packages to update need --update"
        raise typer.BadParameter(msg)
    project = Project.find(arch)
    if not update:
        current = load_lock(project)
        console.print(
            f"{lock_path(project).relative_to(project.root)}: {len(current.packages)} upstream packages, "
            f"{len(current.image)} in the build environment image, build roots of {len(current.buildroots)} packages"
        )
        return
    with project.lock():
        current = load_lock(project)
        names = sorted(n for n, p in current.packages.items() if not packages or {n, p.source} & set(packages))
        if unknown := sorted(set(packages or []) - {x for n in names for x in (n, current.packages[n].source)}):
            msg = f"not locked: {', '.join(unknown)}"
            raise typer.BadParameter(msg)
        latest = latest_versions(project, names)
        current = load_lock(project)  # building the image may have changed it
        changed = [n for n in names if n in latest and latest[n].evr != current.packages[n].evr]
        previous = {name: current.packages[name].evr for name in changed}
        for name in changed:
            console.print(f"[bold]{name}[/] {current.packages[name].evr} -> {latest[name].evr}")
            current.packages[name] = latest[name]
        current.note_updates(previous)
        save_lock(project, current)
    console.print(f"{len(changed)} updated" + (", see xcraft impacts" if current.pending else ""))
