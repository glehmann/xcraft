"""xcraft publish."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from xcraft import build as builder
from xcraft import publish as publisher
from xcraft import remote
from xcraft.commands import complete_arch, load
from xcraft.display import console
from xcraft.signing import dev_signer, release_signer

if TYPE_CHECKING:
    from xcraft.project import Project
    from xcraft.signing import Signer


def publish(  # noqa: PLR0913, PLR0917
    locked: Annotated[
        bool, typer.Option("--locked", help="Fail when publishing would change the lock, for CI.")
    ] = False,
    local: Annotated[
        bool, typer.Option("--local", help="Only make the snapshot locally, in --output, without publishing it.")
    ] = False,
    output: Annotated[
        Path | None, typer.Option(help="Directory of the local snapshot. .xcraft/publish/ by default.")
    ] = None,
    unsigned: Annotated[
        bool,
        typer.Option(
            "--unsigned",
            help="Publish without signing. The packages are signed with the release key of xcraft.toml by default, "
            "or with the developer's key with --local, or to a storage on this machine.",
        ),
    ] = False,
    dev_key: Annotated[
        bool, typer.Option("--dev-key", help="Sign with the developer's key, for a test storage.")
    ] = False,
    arch: Annotated[
        str | None,
        typer.Option(
            help="RPM architecture to publish, like aarch64, or all. By default, all of them, but for a --local "
            "snapshot, the one of $XCRAFT_ARCH, or the host's.",
            show_default=False,
            autocompletion=lambda incomplete: [
                a for a in [*complete_arch(incomplete), "all"] if a.startswith(incomplete)
            ],
        ),
    ] = None,
) -> None:
    """Publish a snapshot of the distribution in the remote storage, as the latest one, for all the architectures.

    It has all our packages, built or fetched first, and the upstream packages they need at runtime, and the extra
    ones of xcraft.toml, as locked, and it must be self-contained. Those of the architectures the host emulates are
    only fetched, built by native builders.
    """
    project = load(locked=locked, arch=None if arch == "all" else arch)
    platforms = project.all_platforms() if arch == "all" or (arch is None and not local) else [project]
    if arch is None and len(platforms) < len(others := project.all_platforms()):
        console.print(
            f"[yellow]warning[/]: only {project.arch} is published, not "
            f"{', '.join(p.arch for p in others if p.arch != project.arch)}: --arch all publishes them all"
        )
    if not (local or project.config.remote):
        msg = "no remote storage to publish to: see [remote] in xcraft.toml, or use --local"
        raise typer.BadParameter(msg)
    if not project.config.publish.release:
        msg = "no release to publish: see [publish] in xcraft.toml"
        raise typer.BadParameter(msg)
    signer = choose_signer(project, local=local, unsigned=unsigned, dev_key=dev_key)
    with project.lock():
        dest = (output or project.state / "publish").absolute()
        built = snapshot(platforms, dest, signer=signer, push=not local, build=project.config.publish.build)
        if not local and (storage := project.config.remote):
            publisher.upload_snapshot(built, dest, publisher.snapshot_name(project, storage), signer=signer)


def choose_signer(project: Project, *, local: bool, unsigned: bool, dev_key: bool) -> Signer | None:
    """Return the signer of a snapshot: the release key, or the developer's.

    That is with `dev_key`, or without release key, for a `local` snapshot, or one published to a storage on this
    machine.
    """
    signer = None if unsigned else dev_signer(project) if dev_key else release_signer(project)
    if dev_key and not signer:
        msg = "no developer's key: run xcraft key create"
        raise typer.BadParameter(msg)
    # a snapshot that stays on this machine, like in the storage of local-s3.sh, can do with the developer's key
    nearby = local or bool((storage := project.config.remote) and remote.on_this_machine(storage))
    if not (signer or unsigned) and nearby and (signer := dev_signer(project)):
        console.print(
            f"[yellow]warning[/]: no release key in xcraft.toml, signing with the developer's key {signer.key}"
            + ("" if local else ", the storage being on this machine")
        )
    if not (signer or local or unsigned):
        msg = "no key to sign the packages: see [sign] in xcraft.toml, or use --dev-key or --unsigned"
        raise typer.BadParameter(msg)
    return signer


def snapshot(
    platforms: list[Project], dest: Path, *, signer: Signer | None, push: bool, build: bool
) -> list[tuple[Project, dict[str, str]]]:
    """Make the snapshot of the `platforms` in `dest`, and return them with the keys of their packages.

    Our packages are built or fetched first, but only fetched for the platforms the host emulates. With `build`, the
    snapshot has the build repository too.
    """
    built, upstream = [], {}
    for project in platforms:
        results = builder.build(project, sorted(project.packages), build_missing=project.native)
        if failed := [r.name for r in results if r.failed]:
            msg = f"not built for {project.arch}: {', '.join(failed)}"
            raise RuntimeError(msg)
        keys = builder.build_keys(project)
        upstream[project.arch] = f"{len(publisher.resolve_runtime(project, keys))} upstream ones" + (
            f", and {len(publisher.build_dependencies(project))} to build them" if build else ""
        )
        built.append((project, keys))
    publisher.assemble(built, dest, signer=signer, push=push, build=build)
    console.print(
        f"[bold green]✓[/] snapshot in {dest}: "
        + ", ".join(f"{p.arch}: {len(keys)} packages, {upstream[p.arch]}" for p, keys in built)
    )
    return built
