"""xcraft publish."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from xcraft import publish as publisher
from xcraft.build import update_build_ids
from xcraft.commands import Key, To, choose_signer, complete_arch, prepared_project, remote_storage, sign_builds
from xcraft.display import console
from xcraft.iso import check_iso_config, fetch_isos, push_isos, snapshot_isos
from xcraft.project import Destination, Project, Publish
from xcraft.publish import ISO_DIR, REPO, snapshot
from xcraft.signing import dev_signer, new_dev_key
from xcraft.upload import Uploader

app = typer.Typer()

Locked = Annotated[bool, typer.Option("--locked", help="Fail when publishing would change the lock, for CI.")]
Local = Annotated[
    bool, typer.Option("--local", help="Only make the snapshot locally, in --output, without publishing it.")
]
Output = Annotated[Path | None, typer.Option(help="Directory of the local snapshot. .xcraft/publish/ by default.")]
PublishDev = Annotated[
    str | None,
    typer.Option(
        "--dev",
        metavar="ID",
        help="Publish in dev mode, like the CI of a pull request: with the development builds of ID, see xcraft build "
        "--dev, which are files of the stage only, never pushed as builds, and the final builds and upstream packages "
        "of the main bucket copied from it, all keeping the signatures of their builds, whose keys the stage has.",
        show_default=False,
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
    dev_key: bool,
    key: str | None,
    to: Destination,
    stage: str | None,
    arch: str | None,
    dev: str | None,
) -> None:
    """Publish the repositories and the ISOs of a snapshot, or `only` the repos or the isos, of dev mode with `dev`."""
    if dev is not None and not re.fullmatch(r"[a-zA-Z0-9]{1,16}", dev):
        msg = f"the development build ID must be 1 to 16 letters and digits: {dev}"
        raise typer.BadParameter(msg, param_hint="--dev")
    project = prepared_project(locked=locked, arch=None if arch == "all" else arch)
    if stage is not None:
        try:
            publish = Publish.model_validate(project.config.publish.model_dump() | {"stage": stage})
        except ValidationError as e:
            msg = "stage must start with a letter, digit or underscore and contain only letters, digits, _, - or ."
            raise typer.BadParameter(msg, param_hint="--stage") from e
        project = replace(project, config=project.config.model_copy(update={"publish": publish}))
    # signed always: by --key, [sign] or the developer's key, which a local snapshot, or one of the dev bucket, makes
    # when there is none
    if not (local or dev_key or key or to != "dev" or dev_signer(project)):
        new_dev_key(project)
    sign_builds(
        project,
        key,
        final=False,
        signer=choose_signer(
            project, local=local, unsigned=False, dev_key=dev_key or (not local and to == "dev" and not key), key=key
        ),
    )  # before the platforms, which copy it
    signer = project.signer
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
    with project.lock():
        update_build_ids(project)  # the final builds are numbered by them
        dest = (output or project.state / "publish").absolute()
        build = only != "isos" and project.config.publish.build
        uploader = Uploader(project, storage, signer, build, dev=dev is not None) if storage else None
        # of the development builds of the run, made again by each one, rather than reused
        if uploader and output is None and dev is None and uploader.reuse(platforms, only=only, isos=with_isos):
            return
        console.print("assembling the snapshot")
        built, stored, checkout = snapshot(
            platforms, dest, signer=signer, storage=storage, build=build, dev=dev, keep=dev is not None
        )
        if storage and output is not None:  # all of it, in the directory asked
            for repo in sorted({path.rsplit("/", 1)[0] for path in stored}):
                publisher.materialize(storage, dest, stored, repo)
        for platform in with_isos:
            console.print(f"getting the ISOs for {platform.arch}")
            # made from its packages
            if storage and stored and (dev is not None or not fetch_isos(platform, signer=signer)):
                publisher.materialize(storage, dest, stored, f"{REPO}/{platform.arch}")
            repo = dest / REPO / platform.arch
            for iso in snapshot_isos(platform, repo, checkout, dest / ISO_DIR, signer=signer, dev=dev):
                console.print(f"[bold green]✓[/] {iso}")
        if uploader:
            publisher.check_checkout(project, checkout)  # which the keys of what is uploaded are computed from
            isos = {
                file: source
                for platform in with_isos
                if dev is None  # those of the checkout: of final builds
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
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
    dev: PublishDev = None,
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
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
    dev: PublishDev = None,
) -> None:
    """Publish repositories as repos/<release>/<stage>/<date>/, and as the stage's latest snapshot."""
    _publish("repos", **ctx.params)


@app.command()
def isos(
    ctx: typer.Context,
    locked: Locked = False,
    local: Local = False,
    output: Output = None,
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
    dev: PublishDev = None,
) -> None:
    """Publish signed packages and netinstall ISOs as isos/<release>/<stage>/<date>/, and as the stage's latest ones.

    They are made for the x86_64 architectures, like xcraft iso, in the ISO directory of the snapshot.
    """
    _publish("isos", **ctx.params)
