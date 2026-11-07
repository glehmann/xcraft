"""xcraft unpublish."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Annotated

import typer
from pydantic import ValidationError

from xcraft import remote, remote_pages
from xcraft.commands import To, remote_storage, unprepared_project
from xcraft.display import activity, console
from xcraft.project import INTERNAL, Publish


def unpublish(
    stage: Annotated[str, typer.Option("--stage", help="The stage to remove.", show_default=False)],
    to: To = "dev",
) -> None:
    """Remove a stage from the remote storage, like the CI of a pull request once it is closed: all its snapshots,
    repositories and ISOs, and what xcraft keeps of them, of the release of xcraft.toml.

    Its files are its own, copies of the builds and of the upstream packages, or development builds, which nothing
    else refers to: the other stages, the builds and the upstream copy are left as they are.
    """
    project = unprepared_project()
    try:
        Publish.model_validate(project.config.publish.model_dump() | {"stage": stage})
    except ValidationError as e:
        msg = "stage must start with a letter, digit or underscore and contain only letters, digits, _, - or ."
        raise typer.BadParameter(msg, param_hint="--stage") from e
    storage, release = remote_storage(project, to), project.config.publish.release
    if not release:
        msg = "no release: see [publish] in xcraft.toml"
        raise typer.BadParameter(msg)
    trees = [f"{kind}/{release}/{stage}" for kind in ("repos", "isos")]
    prefixes = [*trees, *(f"{INTERNAL}/inputs/{tree}" for tree in trees)]
    with activity(f"removing the stage {stage}"):
        files = [f"{prefix}/{f}" for prefix in prefixes for f in remote.listing(storage, f"{prefix}/")]
        # with the pages of their directories
        directories = {
            str(d)
            for f in files
            for d in PurePosixPath(f).parents
            if any(f"{d}/".startswith(f"{prefix}/") for prefix in prefixes)
        }
        remote.delete(storage, files + [f"{d}/{remote.INDEX}" for d in sorted(directories)])
        remote_pages.update_pages(project, storage, [f"{kind}/{release}/" for kind in ("repos", "isos")])
    console.print(f"removed {len(files)} files of the stage {stage}" if files else f"no stage {stage} to remove")
