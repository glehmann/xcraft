"""xcraft impacts."""

from __future__ import annotations

from rich.table import Table

from xcraft import build as builder
from xcraft.commands import Arch, load
from xcraft.display import console
from xcraft.lock import load_lock


def impacts(arch: Arch = None) -> None:
    """Show the pending updates: the upstream packages updated since the releases of the packages built with them.

    For each impacted package: ✓ built with the current lock, ✗ not built with it yet.
    Decide with xcraft bump (new release) or xcraft ack (no release needed).
    """
    project = load(arch=arch)
    lock = load_lock(project)
    if not lock.pending:
        console.print("nothing pending")
        return
    keys = builder.build_keys(project)

    def state(package: str) -> str:
        built = package in keys and builder.cached_build(project, package, keys[package]).exists()
        return f"[green]✓[/] {package}" if built else f"[red]✗[/] {package}"

    table = Table("upstream package", "built with", "now", "impacted packages", box=None, header_style="bold")
    for name in sorted({name for entries in lock.pending.values() for name in entries}):
        impacted = sorted(package for package, entries in lock.pending.items() if name in entries)
        previous = sorted({lock.pending[package][name] for package in impacted})
        now = lock.packages[name].evr if name in lock.packages else "-"
        table.add_row(name, ", ".join(previous), now, ", ".join(state(package) for package in impacted))
    console.print(table)
