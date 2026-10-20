"""xcraft ack."""

from __future__ import annotations

from xcraft.commands import Packages
from xcraft.commands.bump import Dep, pending_packages
from xcraft.display import console
from xcraft.lock import load_lock, save_lock
from xcraft.project import Project


def ack(packages: Packages = None, dep: Dep = None) -> None:
    """Acknowledge pending updates without a new release: all those of the given packages, or of the --dep ones.

    Say why in the commit.
    """
    project = Project.find()
    with project.lock():
        lock = load_lock(project)
        names = pending_packages(packages, lock.pending, dep or [])
        for package in names:
            cleared = sorted(
                lock.pending.get(package, {}) if not dep else set(dep) & lock.pending.get(package, {}).keys()
            )
            console.print(f"[bold]{package}[/]: {', '.join(cleared) or 'nothing pending'}")
        lock.clear_pending(names, dep)
        save_lock(project, lock)
