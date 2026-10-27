"""xcraft push."""

from __future__ import annotations

from typing import Annotated

import typer

from xcraft import ccache, remote
from xcraft.cache import BUILD_FILES, build_keys, build_path, cached_build
from xcraft.commands import Arch, Key, Packages, To, load, remote_storage
from xcraft.display import activity, console
from xcraft.project import Project, Remote
from xcraft.signing import Signer, dev_signer, signing_key
from xcraft.sources import cached_source, read_manifest, source_path

app = typer.Typer()


@app.callback(invoke_without_command=True)
def everything(ctx: typer.Context, arch: Arch = None, key: Key = None, to: To = "dev") -> None:
    """Upload to the remote storage what it does not have yet.

    Without a command: the sources and the builds of all the packages, with the pages of the directories of what is
    pushed: xcraft push index makes them all.
    """
    if ctx.invoked_subcommand:
        ctx.default_map = {ctx.invoked_subcommand: {"to": to, **({"key": key} if key else {})}}
        return
    project = load(arch=arch)
    storage = remote_storage(project, to)
    _push_sources(project, storage, None)
    if not_built := _push_builds(project, storage, None, signer=signing_key(project, key)):
        msg = f"not built: {', '.join(not_built)}"
        raise RuntimeError(msg)


@app.command()
def sources(
    packages: Packages = None,
    check: Annotated[
        bool, typer.Option("--check", help="Only fail when the selected bucket misses archives, for CI.")
    ] = False,
    arch: Arch = None,
    to: To = "dev",
) -> None:
    """Upload cached sources to the selected bucket, or only check it with --check."""
    project = load(packages, arch=arch)
    _push_sources(project, remote_storage(project, to), packages, check=check)


def _push_sources(project: Project, storage: Remote, packages: list[str] | None, *, check: bool = False) -> None:
    """Upload cached sources to `storage`, or only check it."""
    archives = {
        source_path(filename, digest): (filename, digest)
        for name in packages or sorted(project.packages)
        if (manifest := project.package_dirs[name] / "sources").exists()
        for filename, digest in sorted(read_manifest(manifest).items())
    }
    with activity("checking the remote storage"):
        present = remote.existing(storage, list(archives))
    missing = [archive for path, archive in sorted(archives.items()) if path not in present]
    if check:
        for filename, _ in missing:
            console.print(f"[red]✗[/] {filename} is not in the remote storage")
        console.print(f"{len(archives) - len(missing)} archives in the remote storage, {len(missing)} missing")
        if missing:
            raise typer.Exit(1)
        return
    if absent := [filename for filename, digest in missing if not cached_source(project, filename, digest)]:
        msg = f"not in the source cache: {', '.join(absent)}"
        raise RuntimeError(msg)
    for filename, digest in missing:
        with activity(f"uploading {filename}"):
            remote.upload(storage, source_path(filename, digest), project.source_cache / digest / filename)
        console.print(f"[green]↑[/] {filename}")
    if missing:
        with activity("making the pages of the directories"):
            remote.update_pages(project, storage, [f"{source_path(f, d).rsplit('/', 1)[0]}/" for f, d in missing])
    console.print(f"{len(missing)} pushed, {len(archives) - len(missing)} already in the remote storage")


@app.command()
def builds(packages: Packages = None, arch: Arch = None, key: Key = None, to: To = "dev") -> None:
    """Upload the current builds of the given packages, all by default. Test builds are never uploaded."""
    project = load(packages, arch=arch)
    if not_built := _push_builds(project, remote_storage(project, to), packages, signer=signing_key(project, key)):
        msg = f"not built: {', '.join(not_built)}"
        raise RuntimeError(msg)


def _push_builds(project: Project, storage: Remote, packages: list[str] | None, *, signer: Signer | None) -> list[str]:
    """Upload the current builds of `packages`, all by default, and return those not built."""
    keys = build_keys(project)
    paths = {name: build_path(project, name, keys[name]) for name in packages or sorted(project.packages)}
    # first: a build of xcraft build --fetch=needed may be in the storage only
    with activity("checking the remote storage"):
        present = [
            name for name, path in paths.items() if remote.tree_exists(storage, path, trusted=dev_signer(project))
        ]
    not_built = [n for n in paths if n not in present and not cached_build(project, n, keys[n]).exists()]
    pushed = [name for name in paths if name not in present and name not in not_built]
    for name in pushed:
        with activity(f"uploading the build of {name}"):
            remote.upload_tree(
                storage,
                paths[name],
                cached_build(project, name, keys[name]),
                BUILD_FILES,
                signer=signer,
            )
        console.print(f"[green]↑[/] {name}")
    if pushed:
        with activity("making the pages of the directories"):
            remote.update_pages(project, storage, [f"{paths[name]}/" for name in pushed])
    console.print(f"{len(pushed)} pushed, {len(present)} already in the remote storage")
    return not_built


@app.command("ccache")
def caches(packages: Packages = None, arch: Arch = None, key: Key = None, to: To = "dev") -> None:
    """Upload the compiler caches of the given packages, all by default, replacing those of the remote storage, for CI.

    They are not uploaded by xcraft push without a command: they are large, and change with each build.
    """
    project = load(packages, arch=arch)
    storage = remote_storage(project, to)
    pushed = [
        name
        for name in packages or sorted(project.packages)
        if ccache.push(project, storage, name, signer=signing_key(project, key))
    ]
    for name in pushed:
        console.print(f"[green]↑[/] {name}")
    console.print(f"{len(pushed)} pushed")


@app.command()
def index(to: To = "dev") -> None:
    """Make the index.html pages of all the directories of the remote storage, which does not list them."""
    project = Project.find()
    storage = remote_storage(project, to)
    with activity("making the pages of the directories"):
        count = remote.update_pages(project, storage, [""])
    console.print(f"{count} pages")
