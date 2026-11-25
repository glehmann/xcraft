"""xcraft reuse."""

from __future__ import annotations

import typer

from xcraft import build as builder
from xcraft import buildid
from xcraft.cache import build_keys, cached_build
from xcraft.commands import BuiltAgainst, Packages, prepared_project
from xcraft.display import console
from xcraft.project import Project
from xcraft.resolution import upstream_sources


def _ours(project: Project, names: list[str]) -> dict[str, str]:
    """Return those of `names` that are packages of ours, by package or binary name, with their packages."""
    binaries = {b.name: name for name, package in project.packages.items() for b in package.binaries}
    return {n: n if n in project.packages else binaries[n] for n in names if n in project.packages or n in binaries}


def _missing(project: Project, names: list[str]) -> dict[str, list[str]]:
    """Return the architectures of the project whose builds of `names` reused are neither cached nor in the remote
    storage, by name: a reused build is never made again, and fails to build there, as the main branch never had it.
    Not those whose specs exclude an architecture.
    """
    keys, missing = build_keys(project), dict[str, list[str]]()
    for platform in project.all_platforms():
        wanted = [
            n
            for n in names
            if n in platform.parsed
            and not platform.parsed[n].excluded
            and not cached_build(platform, n, keys[n]).exists()
        ]
        found = builder.stored(platform, wanted, keys) if wanted and project.config.remote else {}
        for name in wanted:
            if not found.get(f"{name}/{keys[name]}"):
                missing.setdefault(name, []).append(platform.arch)
    return missing


def reuse(packages: Packages = None, built_against: BuiltAgainst = None) -> None:
    """Reuse the builds of the main branch despite changes, rather than building them again.

    Those of the given packages, whatever changed but their own directories, and of those whose only changes are
    updates of the --built-against packages: upstream ones, like an ABI compatible security fix, or ours they are built
    against, like a fix of xen leaving its interfaces as they are. Their build IDs accept the changes, until they change
    again. The packages built against them keep their builds too, unless they changed otherwise. Say why in the commit.
    """
    if not (packages or built_against):
        msg = "no package given, nor --built-against"
        raise typer.BadParameter(msg)
    packages = packages or []
    project = prepared_project(packages)
    with project.lock():
        ours = _ours(project, built_against or [])
        # first, as it may lock them
        upstream = [d for d in built_against or [] if d not in ours]
        sources = upstream_sources(project, upstream) if upstream else []
        changed = buildid.reuse(project, packages, sources, sorted({*ours.values()}))
        ids, branch = buildid.read(project), buildid.fork_branch(project)
        forked = base.ids if (base := buildid.base(project)) else {}
        reused = sorted(n for n in {*changed, *packages} if (i := ids.get(n)) and i.accepts)
        missing = _missing(project, reused) if reused else {}
        for name in sorted({*changed, *packages}):
            if not (i := ids.get(name)):
                continue
            if i.accepts and (arches := missing.get(name)):
                console.print(
                    f"[yellow]warning[/]: {name}: reuses its build {i.build}, which {branch} has not for "
                    f"{', '.join(arches)}, neither cached nor in the remote storage: it fails to build there, as a "
                    "reused build is never made again; change its release rather than reuse it"
                )
            elif i.accepts:
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
