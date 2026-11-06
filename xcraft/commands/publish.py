"""xcraft publish."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from xcraft import publish as publisher
from xcraft.build import update_build_ids
from xcraft.commands import Key, To, choose_signer, complete_arch, prepared_project, remote_storage
from xcraft.display import console
from xcraft.iso import check_iso_config, fetch_isos, push_isos, snapshot_isos
from xcraft.project import Destination, Project, Publish
from xcraft.publish import ISO_DIR, REPO, snapshot
from xcraft.upload import Uploader

app = typer.Typer()

Locked = Annotated[bool, typer.Option("--locked", help="Fail when publishing would change the lock, for CI.")]
Local = Annotated[
    bool, typer.Option("--local", help="Only make the snapshot locally, in --output, without publishing it.")
]
Output = Annotated[Path | None, typer.Option(help="Directory of the local snapshot. .xcraft/publish/ by default.")]
Unsigned = Annotated[
    bool,
    typer.Option(
        "--unsigned",
        help="Publish without package signing. By default, use --key, \\[sign], or the developer's key. "
        "Remote manifests still need a signing key.",
    ),
]
DevKey = Annotated[bool, typer.Option("--dev-key", help="Sign with the developer's key, for a test storage.")]
Stage = Annotated[
    str | None,
    typer.Option("--stage", help="Stage subdirectory for repositories and ISOs. Defaults to the current user name."),
]
Arch = Annotated[
    str | None,
    typer.Option(
        help="RPM architecture to publish, like aarch64, or all. By default, all of them, but for a --local "
        "snapshot, the one of $XCRAFT_ARCH, or the host's.",
        show_default=False,
        autocompletion=lambda incomplete: [a for a in [*complete_arch(incomplete), "all"] if a.startswith(incomplete)],
    ),
]


def _iso_platforms(project: Project, platforms: list[Project], only: str | None) -> list[Project]:
    """Return the `platforms` of the ISOs to publish, unless `only` the repos: those of x86_64, for their boot loaders,
    when [iso] has the installer's packages.
    """
    if only == "isos" and not project.config.iso.installer:
        msg = "no ISO to publish: see [iso] in xcraft.toml"
        raise typer.BadParameter(msg)
    if only == "repos" or not project.config.iso.installer:
        return []
    if not (found := [p for p in platforms if p.arch.startswith("x86_64")]) and only == "isos":
        msg = f"the ISOs are for x86_64, not {', '.join(p.arch for p in platforms)}"
        raise typer.BadParameter(msg)
    for platform in found:  # before the snapshot
        check_iso_config(platform)
    return found


def _publish(  # noqa: C901, PLR0912
    only: str | None,
    *,
    locked: bool,
    local: bool,
    output: Path | None,
    unsigned: bool,
    dev_key: bool,
    key: str | None,
    to: Destination,
    stage: str | None,
    arch: str | None,
) -> None:
    """Publish the repositories and the ISOs of a snapshot, or `only` the repos or the isos."""
    project = prepared_project(locked=locked, arch=None if arch == "all" else arch)
    if stage is not None:
        try:
            publish = Publish.model_validate(project.config.publish.model_dump() | {"stage": stage})
        except ValidationError as e:
            msg = "stage must start with a letter, digit or underscore and contain only letters, digits, _, - or ."
            raise typer.BadParameter(msg, param_hint="--stage") from e
        project = replace(project, config=project.config.model_copy(update={"publish": publish}))
    platforms = project.all_platforms() if arch == "all" or (arch is None and not local) else [project]
    if arch is None and len(platforms) < len(others := project.all_platforms()):
        console.print(
            f"[yellow]warning[/]: only {project.arch} is published, not "
            f"{', '.join(p.arch for p in others if p.arch != project.arch)}: --arch all publishes them all"
        )
    storage = None if local else remote_storage(project, to)
    if not project.config.publish.release:
        msg = "no release to publish: see [publish] in xcraft.toml"
        raise typer.BadParameter(msg)
    with_isos = _iso_platforms(project, platforms, only)
    signer = choose_signer(
        project,
        local=local,
        unsigned=unsigned,
        dev_key=dev_key or (not local and to == "dev" and not key and not unsigned),
        key=key,
    )
    with project.lock():
        update_build_ids(project)  # the final builds are numbered by them
        dest = (output or project.state / "publish").absolute()
        build = only != "isos" and project.config.publish.build
        uploader = Uploader(project, storage, signer, build) if storage else None
        if uploader and output is None and uploader.reuse(platforms, only=only, isos=with_isos):
            return
        console.print("assembling the snapshot")
        built, stored, checkout = snapshot(platforms, dest, signer=signer, storage=storage, build=build)
        if storage and output is not None:  # all of it, in the directory asked
            for repo in sorted({path.rsplit("/", 1)[0] for path in stored}):
                publisher.materialize(storage, dest, stored, repo)
        for platform in with_isos:
            console.print(f"getting the ISOs for {platform.arch}")
            if storage and stored and not fetch_isos(platform, signer=signer):  # made from its packages
                publisher.materialize(storage, dest, stored, f"{REPO}/{platform.arch}")
            for iso in snapshot_isos(platform, dest / REPO / platform.arch, checkout, dest / ISO_DIR, signer=signer):
                console.print(f"[bold green]✓[/] {iso}")
        if uploader:
            publisher.check_checkout(project, checkout)  # which the keys of what is uploaded are computed from
            isos = {
                file: source
                for platform in with_isos
                for file, source in push_isos(platform, uploader.storage, signer=signer).items()
            }
            name = uploader.snapshot_name()
            if only != "isos":
                uploader.upload_snapshot(built, dest, name, stored=stored)
            if with_isos:
                uploader.upload_isos(dest, name, stored=isos)


@app.callback(invoke_without_command=True)
def everything(
    ctx: typer.Context,
    locked: Locked = False,
    local: Local = False,
    output: Output = None,
    unsigned: Unsigned = False,
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
) -> None:
    r"""Publish a snapshot of the distribution in the remote storage, as the latest one, for all the architectures.

    Without a command: its repositories, and its ISOs, when \[iso] in xcraft.toml has the installer's packages. It has
    all our packages, built or fetched first, and the upstream packages they need at runtime, and the extra ones of
    xcraft.toml, as locked, and it must be self-contained. Those of the architectures the host emulates are only
    fetched, built by native builders.
    """
    if ctx.invoked_subcommand:
        # all the options given before the subcommand are its defaults: those given after it win
        ctx.default_map = {ctx.invoked_subcommand: {k: v for k, v in ctx.params.items() if v is not None}}
        return
    _publish(None, **ctx.params)


@app.command()
def repos(
    ctx: typer.Context,
    locked: Locked = False,
    local: Local = False,
    output: Output = None,
    unsigned: Unsigned = False,
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
) -> None:
    """Publish repositories as repos/<release>/<stage>/<date>/, and as the stage's latest snapshot."""
    _publish("repos", **ctx.params)


@app.command()
def isos(
    ctx: typer.Context,
    locked: Locked = False,
    local: Local = False,
    output: Output = None,
    unsigned: Unsigned = False,
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
) -> None:
    """Publish signed packages and netinstall ISOs as isos/<release>/<stage>/<date>/, and as the stage's latest ones.

    They are made for the x86_64 architectures, like xcraft iso, in the ISO directory of the snapshot.
    """
    _publish("isos", **ctx.params)
