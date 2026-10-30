"""xcraft push."""

from __future__ import annotations

from typing import Annotated

import typer

from xcraft import ccache, remote
from xcraft.cache import BUILD_FILES, build_keys, build_path, cached_build
from xcraft.commands import Arch, Final, Key, Packages, To, load, remote_storage
from xcraft.devbuilds import developed
from xcraft.display import activity, console
from xcraft.iso import push_isos
from xcraft.project import Project, Remote
from xcraft.signing import Signer, dev_signer, signing_key
from xcraft.sources import cached_source, read_manifest, source_path

app = typer.Typer()


@app.callback(invoke_without_command=True)
def everything(ctx: typer.Context, arch: Arch = None, key: Key = None, to: To = "dev") -> None:
    """Upload to the remote storage what it does not have yet.

    Without a command: the sources and the builds of all the packages, with the pages of the directories of what is
    pushed: xcraft push index makes them all. The sources not in the source cache, like those of the packages which did
    not build, and the packages not built, do not keep the others from being pushed, then fail it.
    """
    if ctx.invoked_subcommand:
        ctx.default_map = {ctx.invoked_subcommand: {"to": to, **({"key": key} if key else {})}}
        return
    project = load(arch=arch)
    storage = remote_storage(project, to)
    absent = _push_sources(project, storage, None)
    not_built = _push_builds(project, storage, None, signer=signing_key(project, key), final=False)
    if problems := [
        *([f"not in the source cache: {', '.join(absent)}"] if absent else []),
        *([f"not built: {', '.join(not_built)}"] if not_built else []),
    ]:
        raise RuntimeError("; ".join(problems))


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
    if absent := _push_sources(project, remote_storage(project, to), packages, check=check):
        msg = f"not in the source cache: {', '.join(absent)}"
        raise RuntimeError(msg)


def _push_sources(project: Project, storage: Remote, packages: list[str] | None, *, check: bool = False) -> list[str]:
    """Upload cached sources to `storage`, or only check it, and return those missing from it and the source cache."""
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
        return []
    absent = [archive for archive in missing if not cached_source(project, *archive)]
    missing = [archive for archive in missing if archive not in absent]
    for filename, digest in missing:
        with activity(f"uploading {filename}"):
            remote.upload(storage, source_path(filename, digest), project.source_cache / digest / filename)
        console.print(f"[green]↑[/] {filename}")
    if missing:
        with activity("making the pages of the directories"):
            remote.update_pages(project, storage, [f"{source_path(f, d).rsplit('/', 1)[0]}/" for f, d in missing])
    console.print(f"{len(missing)} pushed, {len(archives) - len(missing) - len(absent)} already in the remote storage")
    return [filename for filename, _ in absent]


@app.command()
def builds(packages: Packages = None, final: Final = False, arch: Arch = None, key: Key = None, to: To = "dev") -> None:
    """Upload the current builds of the given packages, all by default.

    Development builds are never uploaded: the packages with ones are skipped, unless --final, which uploads their
    final builds.
    """
    project = load(packages, arch=arch)
    storage, signer = remote_storage(project, to), signing_key(project, key)
    if not_built := _push_builds(project, storage, packages, signer=signer, final=final):
        msg = f"not built: {', '.join(not_built)}"
        raise RuntimeError(msg)


@app.command()
def isos(
    arch: Arch = None,
    key: Key = None,
    to: To = "dev",
    unsigned: Annotated[
        bool, typer.Option("--unsigned", help="Upload cached ISOs made without package signing.")
    ] = False,
) -> None:
    """Upload cached full and netinstall ISOs matching the checkout, without building them."""
    project = Project.find(arch)
    with project.lock():
        push_isos(
            project,
            remote_storage(project, to),
            signer=None if unsigned else signing_key(project, key),
            upload_signer=signing_key(project, key),
        )


def _push_builds(
    project: Project, storage: Remote, packages: list[str] | None, *, signer: Signer | None, final: bool
) -> list[str]:
    """Upload the current builds of `packages`, all by default, and return those not built: the final builds, but
    of the packages with development builds, unless `final`.
    """  # noqa: D205
    keys, skipped = build_keys(project), set() if final else developed(project)
    if shown := sorted(skipped & {*(packages or project.packages)}):
        console.print(f"development builds, not pushed: {', '.join(shown)}")
    names = [name for name in packages or sorted(project.packages) if name not in skipped]
    paths = {name: build_path(project, name, keys[name]) for name in names}
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
    """Update the compiler caches of the remote storage with those of the given packages, all by default, for CI, with
    the pages of their directories.

    They are not uploaded by xcraft push without a command: they are large, and change with each build.
    """  # noqa: D205
    project = load(packages, arch=arch)
    storage = remote_storage(project, to)
    pushed = [
        name
        for name in packages or sorted(project.packages)
        if ccache.push(project, storage, name, signer=signing_key(project, key))
    ]
    for name in pushed:
        console.print(f"[green]↑[/] {name}")
    if pushed:
        with activity("making the pages of the directories"):
            remote.update_pages(project, storage, [f"{ccache.remote_path(project, name)}/" for name in pushed])
    console.print(f"{len(pushed)} pushed")


@app.command()
def index(to: To = "dev") -> None:
    """Make the index.html pages of all the directories of the remote storage, which does not list them."""
    project = Project.find()
    storage = remote_storage(project, to)
    with activity("making the pages of the directories"):
        count = remote.update_pages(project, storage, [""])
    console.print(f"{count} pages")
