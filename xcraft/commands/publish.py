"""xcraft publish."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft import publish as publisher
from xcraft.commands import choose_signer, complete_arch, load
from xcraft.display import console
from xcraft.publish import snapshot


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
