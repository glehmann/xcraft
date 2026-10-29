"""xcraft reuse."""

from __future__ import annotations

import typer

from xcraft import buildid
from xcraft.commands import Dep, Packages, load
from xcraft.display import console
from xcraft.resolution import upstream_sources


def reuse(packages: Packages = None, dep: Dep = None) -> None:
    """Reuse the builds of the main branch despite changes, rather than building them again.

    Those of the given packages, whatever changed but their own directories, and of those whose only changes are
    updates of the --dep upstream packages, like an ABI compatible security fix. Their build IDs accept the changes,
    until they change again. The packages built against them keep their builds too, unless they changed otherwise.
    Say why in the commit.
    """
    if not (packages or dep):
        msg = "no package given, nor --dep"
        raise typer.BadParameter(msg)
    project = load(packages)
    with project.lock():
        # first, as it may lock them
        sources = upstream_sources(project, dep) if dep else []
        changed = buildid.reuse(project, packages or [], sources)
        ids = buildid.read(project)
        for name in sorted({*changed, *(packages or [])}):
            if name in ids and ids[name].accepts:
                console.print(f"[bold]{name}[/]: reuses its build {ids[name].number}")
            elif name in (packages or []):
                console.print(f"[yellow]{name}[/]: changed itself, built again")
            elif name in ids:
                console.print(f"{name}: build {ids[name].number}")
