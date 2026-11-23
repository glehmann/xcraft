"""xcraft prune."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

import typer

from xcraft import prune as pruner
from xcraft.commands import To, remote_storage, unprepared_project
from xcraft.display import activity, amount, console


def prune(
    keep_days: Annotated[
        int,
        typer.Option(
            "--keep-days", min=0, help="Keep the builds pushed in the last DAYS days, published or not.", metavar="DAYS"
        ),
    ] = pruner.GRACE,
    dry_run: Annotated[bool, typer.Option("-n", "--dry-run", help="Show what would be removed.")] = False,
    to: To = "dev",
) -> None:
    """Remove from the remote storage the builds whose packages no publication has, like a scheduled job of CI.

    What is published is the reference: the builds of the snapshots of the repositories of all the stages, dated or
    latest, are kept, a starting point for a fix, as long as the snapshots are, see snapshot_days; and so are those
    pushed in the last --keep-days days, for a publication failing. The files of the generations of the trees their
    manifests do not have anymore go too. It must not run during a publication, which may copy a build it removes.
    """
    project = unprepared_project()
    storage = remote_storage(project, to)
    with activity("finding the builds no publication has"):
        found = pruner.pruned(storage, keep_days, dt.datetime.now(dt.UTC))
    for tree in found.trees:
        console.print(f"{'would remove' if dry_run else 'removing'} {tree}")
    summary = f"{len(found.trees)} builds, {len(found.files)} files, {amount(found.size)}"
    if dry_run:
        console.print(f"would remove {summary}")
        return
    with activity(f"removing {summary}"):
        pruner.prune(project, storage, found)
    console.print(f"[bold green]✓[/] removed {summary}")
