"""xcraft reuse."""

from __future__ import annotations

import typer

from xcraft import buildid
from xcraft.commands import Dep, Packages, prepared_project
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
    packages = packages or []
    project = prepared_project(packages)
    with project.lock():
        # first, as it may lock them
        sources = upstream_sources(project, dep) if dep else []
        changed = buildid.reuse(project, packages, sources)
        ids, branch = buildid.read(project), buildid.fork_branch(project)
        forked = base.ids if (base := buildid.base(project)) else {}
        for name in sorted({*changed, *packages}):
            if not (i := ids.get(name)):
                continue
            if i.accepts:
                console.print(f"[bold]{name}[/]: reuses its build {i.build}")
            elif i.same(forked.get(name)):
                console.print(f"{name}: has the build of {branch} already, {i.build}")
            elif name in packages and name not in forked:
                console.print(f"[yellow]warning[/]: {name}: no build of {branch} to reuse, a new package")
            elif name in packages:
                console.print(
                    f"[yellow]warning[/]: {name}: its directory changed since {branch}, whose build cannot be reused; "
                    f"its builds keep the number +b{i.build}, only their key follows what they are built against"
                )
            else:
                console.print(f"{name}: build {i.build}")
