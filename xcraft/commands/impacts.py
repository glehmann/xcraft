"""xcraft impacts."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from xcraft import build as builder
from xcraft.commands import Arch, load
from xcraft.display import console
from xcraft.lock import load_lock


def impacts(
    arch: Arch = None,
    markdown: Annotated[bool, typer.Option("--markdown", help="A Markdown table, like for a pull request.")] = False,
) -> None:
    """Show the pending updates: the upstream packages updated since the releases of the packages built with them.

    One row per upstream source package. For each impacted package: ✓ built with the current lock, ✗ not built with it
    yet. Decide with xcraft bump (new release) or xcraft ack (no release needed).
    """
    project = load(arch=arch)
    lock = load_lock(project)
    if not lock.pending:
        console.print("nothing pending")
        return
    keys = builder.build_keys(project)

    def built(package: str) -> bool:
        return package in keys and builder.cached_build(project, package, keys[package]).exists()

    rows = []
    for source in sorted({lock.source(name) for entries in lock.pending.values() for name in entries}):
        names = [name for name in lock.binaries([source]) if any(name in entries for entries in lock.pending.values())]
        impacted = sorted(package for package, entries in lock.pending.items() if set(names) & entries.keys())
        previous = sorted({entries[n] for entries in lock.pending.values() for n in names if n in entries})
        now = sorted({lock.packages[n].evr for n in names if n in lock.packages}) or ["-"]
        if set(names) & set(lock.image):  # in the image: all the packages, but those acknowledged
            count = sum(map(built, impacted))
            states = f"{len(impacted)} packages, through the image: {count} ✓, {len(impacted) - count} ✗"
        else:
            states = ", ".join(f"{'✓' if built(package) else '✗'} {package}" for package in impacted)
        rows.append((source, ", ".join(previous), ", ".join(now), states))
    columns = ("upstream package", "built with", "now", "impacted packages")
    if markdown:
        lines = [columns, ("---",) * 4, *rows]
        console.print("".join(f"| {' | '.join(line)} |\n" for line in lines), end="", markup=False, highlight=False)
        return
    table = Table(*columns, box=None, header_style="bold")
    for source, previous, now, states in rows:
        table.add_row(source, previous, now, states.replace("✓", "[green]✓[/]").replace("✗", "[red]✗[/]"))
    console.print(table)
