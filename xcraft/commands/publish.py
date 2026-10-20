"""xcraft publish."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft import build as builder
from xcraft import publish as publisher
from xcraft import remote
from xcraft.commands import load
from xcraft.display import console
from xcraft.signing import dev_signer, release_signer


def publish(
    locked: Annotated[
        bool, typer.Option("--locked", help="Fail when publishing would change the lock, for CI.")
    ] = False,
    local: Annotated[
        bool, typer.Option("--local", help="Only make the snapshot locally, in --output, without publishing it.")
    ] = False,
    output: Annotated[
        Path | None, typer.Option(help="Directory of the local snapshot. .xcraft/publish/<arch>/ by default.")
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
) -> None:
    """Publish a snapshot of the distribution in the remote storage, as the latest one.

    It has all our packages, built or fetched first, and the upstream packages they need at runtime, and the extra
    ones of xcraft.toml, as locked, and it must be self-contained.
    """
    project = load(locked=locked)
    if not (local or project.config.remote):
        msg = "no remote storage to publish to: see [remote] in xcraft.toml, or use --local"
        raise typer.BadParameter(msg)
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
    with project.lock():
        results = builder.build(project, sorted(project.packages))
        if failed := [r.name for r in results if r.status in ("failed", "skipped")]:
            msg = f"not built: {', '.join(failed)}"
            raise RuntimeError(msg)
        keys = builder.build_keys(project)
        upstream = publisher.resolve_runtime(project, keys)
        dest = (output or project.state / "publish" / project.config.arch).absolute()
        publisher.assemble(project, keys, dest, signer=signer, push=not local)
        console.print(f"[bold green]✓[/] snapshot in {dest}: {len(keys)} packages, {len(upstream)} upstream ones")
        if not local and (storage := project.config.remote):
            publisher.upload_snapshot(project, keys, dest, publisher.snapshot_name(project, storage), signer=signer)
