"""xcraft iso."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft import builders
from xcraft.commands import Arch, Dev, Final, Key, NoCheck, dev_ident, iso_signer, sign_builds
from xcraft.display import console
from xcraft.iso import checkout_iso, checkout_iso_key
from xcraft.project import Project

Local = Annotated[
    bool,
    typer.Option("--local", help="Make it on this machine, rather than on the builder of the architecture, if any."),
]


def made_iso(
    project: Project,
    ident: str | None,
    key: str | None,
    output: Path | None = None,
    *,
    local: bool,
    netinstall: bool = False,
    volid: str | None = None,
    nocheck: bool = False,
) -> Path:
    """Return the ISO of the checkout, see checkout_iso(), made on the builder of the architecture of `project`, if
    any, unless `local`, with the development builds `ident`: natively, as the programs of its packages run to make
    it, emulated here otherwise, and copied back, see builders.iso(); of the developer's key, like its builds.
    """
    signer = iso_signer(project, key)
    sign_builds(project, key, final=ident is None, signer=signer)
    if not local and (host := builders.builders().get(project.arch)):
        if ident is None or key:
            msg = "a builder makes the ISO of the developer's key: --final and --key make it here, with --local"
            raise typer.BadParameter(msg)
        flags = [*(["--netinstall"] if netinstall else []), *(["-V", volid] if volid else [])]
        flags += [*(["--nocheck"] if nocheck else []), *(["--locked"] if project.frozen else [])]
        args = [*flags, "--dev", ident, "--arch", project.arch, "--local"]

        def made() -> str:
            return checkout_iso_key(
                project, netinstall=netinstall, volid=volid, signer=signer, dev=ident, nocheck=nocheck
            )

        with project.lock():
            if code := builders.iso(project, host, args, made):
                raise typer.Exit(code)
    return checkout_iso(
        project,
        output,
        netinstall=netinstall,
        volid=volid,
        signer=signer,
        dev=ident,
        nocheck=nocheck,
        published=not key,  # CI's, rather than one made with the developer's key
    )


def iso(
    ctx: typer.Context,
    netinstall: Annotated[
        bool, typer.Option("--netinstall", help="Without the packages, installed from a repository on the network.")
    ] = False,
    output: Annotated[
        Path | None, typer.Option(help="The ISO. .xcraft/iso/<name>-<version>-<id>-<arch>[-netinstall].iso by default.")
    ] = None,
    volid: Annotated[str | None, typer.Option("-V", help="Volume id. [iso] volid of xcraft.toml by default.")] = None,
    locked: Annotated[
        bool, typer.Option("--locked", help="Fail when making the ISO would change the lock, for CI.")
    ] = False,
    arch: Arch = None,
    key: Key = None,
    dev: Dev = None,
    final: Final = False,
    nocheck: NoCheck = False,
    local: Local = False,
) -> None:
    """Make the installation ISO, from a local snapshot of the distribution, as xcraft publish --local makes it.

    The installer's root filesystem, install.img, comes from the snapshot, which has its packages, and so does the
    repository of the ISO, with [iso] packages and what they need. The hook of the project, iso/hook, makes what is
    particular to the distribution. Like xcraft build, it has the development builds of the
    packages whose build ID is not the main branch's, unless --final.

    It is made on the builder of the architecture, if any, unless --local: natively, rather than with the programs of
    its packages emulated here, then copied back.
    """
    ident = dev_ident(ctx, dev, final=final, nocheck=nocheck)
    project = Project.find(arch)
    project.frozen = locked
    path = made_iso(project, ident, key, output, local=local, netinstall=netinstall, volid=volid, nocheck=nocheck)
    console.print(f"[bold green]✓[/] {path}")
