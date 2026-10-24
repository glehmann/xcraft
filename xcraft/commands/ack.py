"""xcraft ack."""

from __future__ import annotations

from xcraft.commands import Dep, Packages, pending_packages
from xcraft.display import console
from xcraft.lock import load_lock, save_lock
from xcraft.project import Project
from xcraft.resolution import upstream_sources


def ack(packages: Packages = None, dep: Dep = None) -> None:
    """Acknowledge pending updates without a new release: all those of the given packages, or of the --dep ones.

    Say why in the commit.
    """
    project = Project.find()
    with project.lock():
        # first, as it may lock them
        sources = upstream_sources(project, dep) if dep else None
        lock = load_lock(project)
        names = pending_packages(packages, lock.pending, sources or [])
        for package in names:
            entries = lock.pending.get(package, {})
            cleared = sorted(entries if sources is None else set(sources) & entries.keys())
            console.print(f"[bold]{package}[/]: {', '.join(cleared) or 'nothing pending'}")
        lock.clear_pending(names, sources)
        save_lock(project, lock)
