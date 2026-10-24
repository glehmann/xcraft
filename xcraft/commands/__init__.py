"""The xcraft subcommands, one per module, and what they share."""

from __future__ import annotations

import re
from typing import Annotated

import typer

from xcraft import remote
from xcraft.display import console
from xcraft.project import Project, Remote
from xcraft.resolution import prepare
from xcraft.signing import Signer, dev_signer, release_signer


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
    """  # noqa: D205
    project = Project.find(arch)
    project.frozen = locked
    # before preparing, which may run containers
    if unknown := sorted(set(names or []) - project.package_dirs.keys()):
        msg = f"unknown package(s): {', '.join(unknown)}"
        raise typer.BadParameter(msg)
    prepare(project)
    return project


def remote_storage(project: Project) -> Remote:
    """Return the remote storage of the project, which the command needs."""
    if not (storage := project.config.remote):
        msg = "no remote storage: see [remote] in xcraft.toml"
        raise typer.BadParameter(msg)
    return storage


Test = Annotated[
    str | None,
    typer.Option(
        metavar="ID",
        help="Test builds of the given packages, above their release and below the next one: <release>.0.ID.<n>.",
    ),
]
Pre = Annotated[
    str | None,
    typer.Option(metavar="ID", help="Pre builds of the given packages, below their release: <release>~ID.<n>."),
]


def test_kind(test: str | None, pre: str | None, packages: list[str] | None) -> tuple[str, str] | None:
    """Return the kind and ID of the test builds of `packages` asked with --test or --pre, if any."""
    if test and pre:
        msg = "--test and --pre cannot be used together"
        raise typer.BadParameter(msg)
    if (test or pre) and not packages:
        msg = "test and pre builds are of the given packages only"
        raise typer.BadParameter(msg)
    if not (ident := test or pre):
        return None
    if not re.fullmatch(r"[a-zA-Z0-9]{1,16}", ident):
        msg = f"the build ID must be 1 to 16 letters and digits: {ident}"
        raise typer.BadParameter(msg)
    return ("test" if test else "pre"), ident


Dep = Annotated[
    list[str] | None,
    typer.Option(
        "--dep",
        help="Upstream package, binary or source name: select the packages with a pending update of it.",
        show_default=False,
    ),
]


def pending_packages(names: list[str] | None, pending: dict[str, dict[str, str]], upstream: list[str]) -> list[str]:
    """Return the packages `names`, and those with pending updates of the `upstream` packages."""
    selected = set(names or []) | {package for package, entries in pending.items() if set(upstream) & entries.keys()}
    if not selected:
        msg = "no package given, or pending on the given upstream packages"
        raise typer.BadParameter(msg)
    return sorted(selected)


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
