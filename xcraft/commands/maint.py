"""xcraft maint."""

from __future__ import annotations

import subprocess
from typing import Annotated

import typer

from xcraft import remote
from xcraft.buildid import maintenance
from xcraft.commands import remote_storage
from xcraft.display import console
from xcraft.process import git
from xcraft.project import Project
from xcraft.upload import INPUTS, SnapshotInputs, inputs_tree

app = typer.Typer(help="The maintenance branches of the releases, for their fixes: see docs/embargo-design.md.")


def _released(project: Project, release: str, stage: str) -> str:
    """Return the commit the latest snapshot of the repositories of `stage` of `release` was made from, in the main
    remote storage, signed by one of its keys.
    """
    storage, latest = remote_storage(project, "main", write=False), f"repos/{release}/{stage}/latest"
    with project.tmp_dir("maint-inputs") as work:
        if not remote.download_tree(storage, inputs_tree(latest), work / "inputs"):
            msg = f"no published snapshot in {latest}, signed by a key of remote.main"
            raise RuntimeError(msg)
        if not (commit := SnapshotInputs.model_validate_json((work / "inputs" / INPUTS).read_bytes()).commit):
            msg = f"the commit of {latest} is unknown, published before it was recorded: give it with --at"
            raise RuntimeError(msg)
        if commit.endswith("-dirty"):
            msg = f"{latest} was published from a checkout with changes, {commit}: give its commit with --at"
            raise RuntimeError(msg)
    return commit


@app.command()
def start(
    release: Annotated[str | None, typer.Argument(help="The release, the one of [publish] by default.")] = None,
    stage: Annotated[str, typer.Option(help="The stage of the snapshots of the release users have.")] = "stable",
    at: Annotated[
        str | None, typer.Option(help="The commit of the release, rather than that of its latest snapshot.")
    ] = None,
    name: Annotated[
        str | None,
        typer.Option(help="The name of the branch, of a maintenance_branches pattern, like embargo/xsa-123."),
    ] = None,
) -> None:
    """Make the maintenance branch of a release, maint/<release> by default, from the first pattern of
    maintenance_branches, at the commit its latest snapshot was made from: the branches forked from it get maintenance
    builds, numbered after the release's, see docs/builds.md.
    """
    project = Project.find()
    if not project.config.maintenance_branches:
        msg = "no maintenance branches: see maintenance_branches in xcraft.toml"
        raise RuntimeError(msg)
    release = release or project.config.publish.release
    commit = at or _released(project, release, stage)
    branch = name or project.config.maintenance_branches[0].replace("*", release)
    repo = str(project.root)
    try:
        git("-C", repo, "cat-file", "-e", f"{commit}^{{commit}}")
    except subprocess.CalledProcessError as e:
        msg = f"the commit of the release, {commit}, is not in the repository: fetch it"
        raise RuntimeError(msg) from e
    if git("-C", repo, "for-each-ref", f"refs/heads/{branch}", f"refs/remotes/origin/{branch}"):
        msg = f"{branch} exists already"
        raise RuntimeError(msg)
    git("-C", repo, "branch", branch, commit)
    if branch not in maintenance(project):
        git("-C", repo, "branch", "-D", branch)
        msg = f"{branch} is of none of the maintenance_branches: {', '.join(project.config.maintenance_branches)}"
        raise RuntimeError(msg)
    console.print(f"[bold green]✓[/] {branch}, from {commit[:12]}: its fixes go to branches of it, merged into it")
