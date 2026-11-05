"""xcraft iso."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft.commands import Arch, Dev, Final, Key, choose_signer, dev_ident
from xcraft.display import console
from xcraft.iso import checkout_iso
from xcraft.project import Project


def iso(
    ctx: typer.Context,
    netinstall: Annotated[
        bool, typer.Option("--netinstall", help="Without the packages, installed from a repository on the network.")
    ] = False,
    output: Annotated[
        Path | None, typer.Option(help="The ISO. .xcraft/iso/xcp-ng-<version>[-netinstall].iso by default.")
    ] = None,
    volid: Annotated[str | None, typer.Option("-V", help="Volume id. XCP-NG_<version> by default.")] = None,
    locked: Annotated[
        bool, typer.Option("--locked", help="Fail when making the ISO would change the lock, for CI.")
    ] = False,
    unsigned: Annotated[
        bool,
        typer.Option(
            "--unsigned",
            help="Without signing the packages and the repository. They are signed with the release key of "
            "xcraft.toml by default, or with the developer's key.",
        ),
    ] = False,
    arch: Arch = None,
    key: Key = None,
    dev: Dev = None,
    final: Final = False,
) -> None:
    """Make the installation ISO, from a local snapshot of the distribution, as xcraft publish --local makes it.

    The installer's root filesystem, install.img, comes from the snapshot, which has its packages, and so does the
    repository of the ISO, with xcp-ng-deps and what it needs. Like xcraft build, it has the development builds of the
    packages whose build ID is not the main branch's, unless --final.
    """
    ident = dev_ident(ctx, dev, final=final)
    project = Project.find(arch)
    project.frozen = locked
    signer = choose_signer(project, local=True, unsigned=unsigned, dev_key=False, key=key)
    path = checkout_iso(project, output, netinstall=netinstall, volid=volid, signer=signer, dev=ident)
    console.print(f"[bold green]✓[/] {path}")
