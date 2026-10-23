"""xcraft lock."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft.commands import Arch
from xcraft.container import available_versions, build_image
from xcraft.display import console
from xcraft.lock import Available, Lock, Locked, choose_versions, differences, load_lock, lock_path, save_lock
from xcraft.project import Project


def lock(  # noqa: PLR0913, PLR0917
    packages: Annotated[
        list[str] | None,
        typer.Argument(help="Upstream packages to update, binary or source names. All by default.", show_default=False),
    ] = None,
    update: Annotated[bool, typer.Option("--update", help="Update locked packages to their latest versions.")] = False,
    outdated: Annotated[
        bool, typer.Option("--outdated", help="Show what --update would change, without changing it, and fail then.")
    ] = False,
    available: Annotated[
        list[Path] | None,
        typer.Option(
            "--available",
            help="Write the available versions of the locked packages of the architecture to this file. With --update "
            "or --outdated, the versions of the files of the architectures, rather than looking for them.",
            show_default=False,
        ),
    ] = None,
    check: Annotated[
        bool, typer.Option("--check", help="Fail when the architectures do not lock the same versions.")
    ] = False,
    arch: Arch = None,
) -> None:
    """Show the locks of upstream packages, of all the architectures, or update them.

    --update takes the latest version all the architectures have, but the suffixes of the releases of release_suffixes
    in xcraft.toml. With --arch, only the lock of this architecture changes, to the versions of the others, starting
    with the packages of its build environment image for a new one. --outdated, for CI, fails when an update would
    change the locks. With --available, CI looks for the versions of each architecture on a machine of it, without
    emulation, and updates the locks of all of them from their files.
    """
    if packages and not (update or outdated or available):
        msg = "packages to update need --update, --outdated or --available"
        raise typer.BadParameter(msg)
    project = Project.find(arch)
    platforms = [p for p in project.all_platforms() if lock_path(p).exists()]
    if arch and project not in platforms and not update:  # --outdated included
        msg = f"no lock for {project.arch} yet: xcraft lock --update --arch {project.arch} makes it"
        raise typer.BadParameter(msg)
    if available and not (update or outdated):
        _write_available(project, available, packages or [])
        return
    if update or outdated:
        with project.lock():
            if arch and project not in platforms:
                build_image(project)  # which locks the packages of a new image
                platforms = [p for p in project.all_platforms() if lock_path(p).exists()]
            given = {
                a.arch: a.versions for a in (Available.model_validate_json(f.read_text()) for f in available or [])
            }
            if unknown := sorted(given.keys() - {p.arch for p in platforms}):
                msg = f"no lock for the architectures of --available: {', '.join(unknown)}"
                raise typer.BadParameter(msg)
            updated = [p for p in platforms if p.arch in given] if given else [project] if arch else platforms
            changed = _update(updated, platforms, packages or [], given or None, write=update)
        if outdated:
            raise typer.Exit(1 if changed else 0)
    if not _show(project, platforms if not arch or update else [project], platforms) and check:
        raise typer.Exit(1)


def _write_available(project: Project, files: list[Path], packages: list[str]) -> None:
    """Write the available versions of the locked `packages` of the project's architecture, all by default, to the
    only one of `files`.
    """  # noqa: D205
    if len(files) > 1:
        msg = "a single --available file to write"
        raise typer.BadParameter(msg)
    with project.lock():
        versions = available_versions(project, _names({project.arch: load_lock(project)}, packages)[project.arch])
    files[0].write_text(Available(arch=project.arch, versions=versions).model_dump_json())
    console.print(f"{files[0]}: the available versions of {len(versions)} packages of {project.arch}")


def _show(project: Project, shown: list[Project], platforms: list[Project]) -> bool:
    """Show the locks of the `shown` platforms, and the versions those of all the `platforms` differ on: return
    whether they are the same.
    """  # noqa: D205
    for platform in shown:
        current = load_lock(platform)
        console.print(
            f"{lock_path(platform).relative_to(project.root)}: {len(current.packages)} upstream packages, "
            f"{len(current.image)} in the build environment image, build roots of {len(current.buildroots)} packages"
        )
    different = differences({p.arch: load_lock(p) for p in platforms}, project.config.release_suffixes)
    for name, evrs in different.items():
        console.print(f"[yellow]{name}[/] " + ", ".join(f"{evr} on {arch}" for arch, evr in evrs.items()))
    if different:
        console.print(
            f"{len(different)} packages locked in other versions on some architectures: xcraft lock --update aligns "
            "them, or with --arch, only the lock of an architecture on the others"
        )
    return not different


def _names(locks: dict[str, Lock], packages: list[str]) -> dict[str, list[str]]:
    """Return the locked packages of `packages`, binary or source names, all by default, in each of the `locks`."""
    names = {
        arch: sorted(n for n, p in lock.packages.items() if not packages or {n, p.source} & set(packages))
        for arch, lock in locks.items()
    }
    known = {x for arch, lock in locks.items() for n in names[arch] for x in (n, lock.packages[n].source)}
    if unknown := sorted(set(packages) - known):
        msg = f"not locked: {', '.join(unknown)}"
        raise typer.BadParameter(msg)
    return names


def _update(
    updated: list[Project],
    platforms: list[Project],
    packages: list[str],
    given: dict[str, dict[str, list[Locked]]] | None,
    *,
    write: bool,
) -> int:
    """Update the locks of the `updated` platforms, for the `packages`, all by default, like the others `platforms`,
    to the available versions `given`, or else looked for, or only show what would change without `write`, and return
    the number of changes.
    """  # noqa: D205
    names = _names({p.arch: load_lock(p) for p in updated}, packages)
    if given is None:  # but for the architectures without these packages
        given = {p.arch: available_versions(p, names[p.arch]) if names[p.arch] else {} for p in updated}
    available = {arch: {n: given[arch][n] for n in names[arch] if n in given[arch]} for arch in names}
    locks = {p.arch: load_lock(p) for p in platforms}  # building the images may have changed them
    chosen, count = choose_versions(locks, available, platforms[0].config.release_suffixes), 0
    for platform in updated:
        current = locks[platform.arch]
        changed = [n for n, p in chosen[platform.arch].items() if p.evr != current.packages[n].evr]
        previous = {name: current.packages[name].evr for name in changed}
        for name in changed:
            new = chosen[platform.arch][name]
            console.print(f"[bold]{name}[/] {current.packages[name].evr} -> {new.evr} on {platform.arch}")
            current.packages[name] = new
        count += len(changed)
        if write:
            current.note_updates(previous)
            save_lock(platform, current)
    if not write:
        console.print(f"{count} to update: xcraft lock --update updates them" if count else "up to date")
    else:
        pending = any(locks[p.arch].pending for p in updated)
        console.print(f"{count} updated" + (", see xcraft impacts" if pending else ""))
    return count
