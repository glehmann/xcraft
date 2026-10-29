"""xcraft status."""

from __future__ import annotations

from collections import Counter

import typer  # noqa: TC002, typer resolves the annotations
from rich.measure import Measurement
from rich.table import Table

from xcraft import build as builder
from xcraft.cache import build_keys, cached_build, has_normal_build, inputs, release_suffix
from xcraft.commands import Arch, Dev, Final, Packages, dev_ident, load
from xcraft.devbuilds import dev_suffixes
from xcraft.display import console

_STYLES = {
    "error": "red",
    "building": "yellow",
    "queued": "cyan",
    "built": "green",
    "outdated": "magenta",
    "missing": "dim",
}


def status(
    ctx: typer.Context, packages: Packages = None, dev: Dev = None, final: Final = False, arch: Arch = None
) -> None:
    """Show the version of the given packages, or all, and whether it is built.

    The version has the build number, and the development build suffix of those whose build ID is not the main
    branch's, unless --final. The states: error (the spec cannot be parsed), building, queued (by a running build),
    built, outdated (only older versions are built), and missing.
    """
    ident = dev_ident(ctx, dev, final=final)
    project = load(packages, arch=arch)
    keys = build_keys(project, dev_suffixes(project, ident), hashes=inputs(project))
    queue = [name for name in builder.queued(project) if not cached_build(project, name, keys[name]).exists()]
    shown = sorted(set(packages or project.packages))
    table = Table("package", "version", "state", box=None, pad_edge=False, header_style="bold")
    states = Counter[str]()
    for name in shown:
        package = project.packages[name]
        evr, suffix, dist = (
            package.binaries[0].evr if package.binaries else "",
            release_suffix(keys[name]),
            package.dist,
        )
        if evr:
            evr = evr.replace(dist, f"{suffix}{dist}") if dist and dist in evr else f"{evr}{suffix}"
        if package.error:
            state = "error"
        elif builder.building(project, name):
            state = "building"
        elif name in queue:
            state = "queued"
        elif cached_build(project, name, keys[name]).exists():
            state = "built"
        else:
            state = "outdated" if has_normal_build(project, name) else "missing"
        states[state] += 1
        detail = f" {queue.index(name) + 1}/{len(queue)}" if state == "queued" else ""
        table.add_row(name, evr, f"[{_STYLES[state]}]{state}{detail}[/]")
    # one line per package, for grep, even wider than the terminal
    width = console.width
    console.width = max(width, Measurement.get(console, console.options.update(width=10_000), table).maximum)
    try:
        console.print(table)
    finally:
        console.width = width
    console.print(", ".join(f"[{_STYLES[state]}]{states[state]} {state}[/]" for state in _STYLES if states[state]))
