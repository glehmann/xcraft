"""The xcraft subcommands, one per module, and what they share."""

from __future__ import annotations

import re
from typing import Annotated

import typer

from xcraft.project import Destination, Project, Remote
from xcraft.resolution import prepare
from xcraft.signing import Signer, dev_signer, signing_key


def complete_package(incomplete: str) -> list[str]:
    """Return the packages starting with `incomplete`, for the shell completion."""
    try:
        return sorted(name for name in Project.find().package_dirs if name.startswith(incomplete))
    except (RuntimeError, OSError):
        return []


Packages = Annotated[
    list[str] | None, typer.Argument(help="Packages.", show_default=False, autocompletion=complete_package)
]
OnePackage = Annotated[str, typer.Argument(help="Package.", show_default=False, autocompletion=complete_package)]
All = Annotated[bool, typer.Option("--all", help="All packages.")]
Key = Annotated[
    str | None, typer.Option("--key", metavar="FINGERPRINT", help="Sign with this GPG key instead of the default key.")
]
To = Annotated[Destination, typer.Option("--to", help="Bucket to upload to.")]


def complete_arch(incomplete: str) -> list[str]:
    """Return the architectures of the project starting with `incomplete`, for the shell completion."""
    try:
        return [p.arch for p in Project.find().all_platforms() if p.arch.startswith(incomplete)]
    except (RuntimeError, OSError):
        return []


Arch = Annotated[
    str | None,
    typer.Option(
        help="RPM architecture to work on, like aarch64. $XCRAFT_ARCH, or the host's, by default.",
        show_default=False,
        autocompletion=complete_arch,
    ),
]


def load(names: list[str] | None = None, *, locked: bool = False, arch: str | None = None) -> Project:
    """Return the project, with its packages and their build dependencies, checking `names` are packages, once the
    lock has the upstream packages the builds need, which it may lock (see resolution.prepare()).

    With `locked`, the lock cannot change: it fails instead.
    """
    project = Project.find(arch)
    project.frozen = locked
    # before preparing, which may run containers
    if unknown := sorted(set(names or []) - project.package_dirs.keys()):
        msg = f"unknown package(s): {', '.join(unknown)}"
        raise typer.BadParameter(msg)
    prepare(project)
    return project


def remote_storage(project: Project, to: Destination = "main") -> Remote:
    """Return the remote storage of the project, which the command needs."""
    if not (storage := project.config.remote.get(to)):
        msg = f"no {to} remote storage: see [remote.{to}] in xcraft.toml"
        raise typer.BadParameter(msg)
    return storage


Dev = Annotated[
    str | None,
    typer.Option(
        "--dev",
        metavar="ID",
        envvar="XCRAFT_DEV",
        help="Identifier of the development builds: of the packages whose build ID is not the main branch's, as "
        "<release>+b<n>~<counter>.ID. dev by default.",
        show_default=False,
    ),
]
Final = Annotated[
    bool, typer.Option("--final", help="Final builds, without development suffixes, like CI on the main branch.")
]


def dev_ident(ctx: typer.Context, dev: str | None, *, final: bool) -> str | None:
    """Return the identifier of the development builds, or None for final builds: --final overrides $XCRAFT_DEV."""
    if final:
        if (source := ctx.get_parameter_source("dev")) and source.name == "COMMANDLINE":  # not $XCRAFT_DEV
            msg = "--final and --dev cannot be used together"
            raise typer.BadParameter(msg)
        return None
    if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", ident := dev or "dev"):
        msg = f"the development build ID must be 1 to 16 letters and digits: {ident}"
        raise typer.BadParameter(msg)
    return ident


Dep = Annotated[
    list[str] | None,
    typer.Option(
        "--dep",
        help="Upstream package, binary or source name: select the packages whose only changes are updates of it.",
        show_default=False,
    ),
]


def choose_signer(
    project: Project, *, local: bool, unsigned: bool, dev_key: bool, key: str | None = None
) -> Signer | None:
    """Select the explicit, release or developer key for a snapshot."""
    if key and dev_key:
        msg = "--key and --dev-key cannot be used together"
        raise typer.BadParameter(msg)
    if unsigned and (key or dev_key):
        msg = "--unsigned cannot be used with --key or --dev-key"
        raise typer.BadParameter(msg)
    signer = None if unsigned else dev_signer(project) if dev_key else signing_key(project, key)
    if dev_key and not signer:
        msg = "no developer's key: run xcraft key create"
        raise typer.BadParameter(msg)
    if not (signer or local or unsigned):
        msg = "no key to sign the packages: run xcraft key create, configure [sign], or use --key or --unsigned"
        raise typer.BadParameter(msg)
    return signer
