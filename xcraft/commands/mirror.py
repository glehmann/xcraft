"""xcraft mirror."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from xcraft import mirror as mirrors
from xcraft.commands import remote_storage, unprepared_project
from xcraft.display import activity, console
from xcraft.project import Destination, Publish
from xcraft.signing import key_signer


def mirror(
    dest: Annotated[Path, typer.Argument(help="The directory of the copy, which an rsync module serves.")],
    stage: Annotated[str, typer.Option("--stage", help="The stage to copy.", show_default=False)],
    check: Annotated[
        bool, typer.Option("--check", help="Check the copy against its manifests, and download the damaged files.")
    ] = False,
    source: Annotated[Destination, typer.Option("--from", help="Bucket to copy from.")] = "main",
) -> None:
    """Copy the latest snapshots of a stage, its repositories and ISOs, for the mirrors to sync from, by rsync.

    The copy has the paths of the storage, each latest snapshot a symlink switched at once to a version checked
    against its signed manifest, made from the public endpoint of the storage, without S3 credentials: a file has the
    date of the storage, so that a copy made again is the same for rsync, and is hard linked from the previous version
    when it is the same. Run after each publication of the stage, and periodically.
    """
    project = unprepared_project()
    try:
        publish = Publish.model_validate(project.config.publish.model_dump() | {"stage": stage})
    except ValidationError as e:
        msg = "stage must start with a letter, digit or underscore and contain only letters, digits, _, - or ."
        raise typer.BadParameter(msg, param_hint="--stage") from e
    project = replace(project, config=project.config.model_copy(update={"publish": publish}))
    storage, trusted = remote_storage(project, source, write=False), key_signer(project)
    if check:
        with activity(f"checking the copy of {stage}"):
            problems = mirrors.checked(project, storage, dest.absolute(), trusted)
        for problem in problems:
            console.print(f"[red]✗[/] {problem}")
        if problems:
            raise typer.Exit(1)
        console.print(f"[bold green]✓[/] the copy of {stage} is as its manifests say")
        return
    with activity(f"copying the latest snapshots of {stage}"):
        changed = mirrors.mirrored(project, storage, dest.absolute(), trusted)
    console.print(f"[bold green]✓[/] copied {', '.join(changed)}" if changed else f"the copy of {stage} is up to date")
