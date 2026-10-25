"""xcraft iso."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft.commands import Arch, Key, choose_signer, load
from xcraft.display import console
from xcraft.iso import make_iso
from xcraft.publish import REPO, snapshot


def iso(  # noqa: PLR0913, PLR0917
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
) -> None:
    """Make the installation ISO, from a local snapshot of the distribution, as xcraft publish --local makes it.

    The installer's root filesystem, install.img, comes from the snapshot, which has its packages, and so does the
    repository of the ISO, with xcp-ng-deps and what it needs.
    """
    project = load(locked=locked, arch=arch)
    signer = choose_signer(project, local=True, unsigned=unsigned, dev_key=False, key=key)
    with project.lock():
        dest = (project.state / "publish").absolute()
        snapshot([project], dest, signer=signer, storage=None, build=False)
        repo = dest / REPO / project.arch
        path = make_iso(project, repo, output, netinstall=netinstall, volid=volid, signer=signer)
    console.print(f"[bold green]✓[/] {path}")
