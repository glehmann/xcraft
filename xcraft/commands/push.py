"""xcraft push."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Annotated

import typer
from rich.markup import escape

from xcraft import ccache, remote, remote_pages, upstream_copy
from xcraft.cache import BUILD_FILES, build_keys, build_path, cached_build, signed_build, untested
from xcraft.commands import Arch, Final, Key, Packages, To, prepared_project, remote_storage, unprepared_project
from xcraft.devbuilds import developed
from xcraft.display import activity, console
from xcraft.iso import push_isos
from xcraft.project import Destination, Project, Remote
from xcraft.resolution import remote_tree, resolutions_read
from xcraft.signing import Signer, key_signer
from xcraft.sources import has_cached_source, read_manifest, sha256_of, source_path

app = typer.Typer()


def _push_sources(project: Project, storage: Remote, packages: list[str] | None, *, check: bool = False) -> list[str]:
    """Upload cached sources to `storage`, or only check it, and return those missing from it and the source cache."""
    archives = {
        source_path(filename, digest): (filename, digest)
        for name in packages or sorted(project.package_dirs)
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
    absent = [archive for archive in missing if not has_cached_source(project, *archive)]
    missing = [archive for archive in missing if archive not in absent]
    for filename, digest in missing:
        with activity(f"uploading {filename}"):
            remote.upload(storage, source_path(filename, digest), project.source_cache / digest / filename)
        console.print(f"[green]↑[/] {filename}")
    if missing:
        with activity("making the pages of the directories"):
            remote_pages.update_pages(project, storage, [f"{source_path(f, d).rsplit('/', 1)[0]}/" for f, d in missing])
    console.print(f"{len(missing)} pushed, {len(archives) - len(missing) - len(absent)} already in the remote storage")
    return [filename for filename, _ in absent]


def _push_builds(
    project: Project, storage: Remote, packages: list[str] | None, *, signer: Signer | None, final: bool
) -> list[str]:
    """Upload the current builds of `packages`, all by default, signed by `signer`, signing again those of other keys,
    and return those not built: the final builds, but of the packages with development builds, unless `final`. The
    builds without the tests of their specs, see xcraft.cache.untested(), are not built for it.
    """
    signer = remote.required_signer(signer)
    keys, skipped = build_keys(project), set() if final else developed(project)
    if shown := sorted(skipped & {*(packages or project.packages)}):
        console.print(f"development builds, not pushed: {', '.join(shown)}")
    names = [name for name in packages or sorted(project.packages) if name not in skipped]
    paths = {name: build_path(project, name, keys[name], signer.key) for name in names}
    # first: a build of xcraft build --fetch=needed may be in the storage only
    with activity("checking the remote storage"), ThreadPoolExecutor(remote.REQUESTS) as pool:
        found = pool.map(
            lambda path: remote.tree_exists(storage, path, trusted=signer, signer=signer.key), paths.values()
        )
        present = [name for name, exists in zip(paths, found, strict=True) if exists]
    if without_tests := [n for n in paths if n not in present and untested(project, n, keys[n])]:
        console.print(f"built without their tests, with --nocheck, not pushed: {', '.join(without_tests)}")
    not_built = [
        n for n in paths if n not in present and (n in without_tests or not cached_build(project, n, keys[n]).exists())
    ]
    pushed = [name for name in paths if name not in present and name not in not_built]
    for name in pushed:
        with activity(f"uploading the build of {name}"):
            build = signed_build(project, name, keys[name], signer)
            remote.upload_tree(storage, paths[name], build, BUILD_FILES, signer=signer)
        console.print(f"[green]↑[/] {name}")
    if pushed:
        with activity("making the pages of the directories"):
            remote_pages.update_pages(project, storage, [f"{paths[name]}/" for name in pushed])
    console.print(f"{len(pushed)} pushed, {len(present)} already in the remote storage")
    return not_built


def _copy(project: Project, source: Remote, storage: Remote) -> None:
    """Copy from `source` to `storage`, server-side, the source archives and the final builds of the packages of the
    checkout `storage` does not have, those signed by a key it trusts: the builds of a maintenance branch, made in
    private, released as they are, see docs/embargo-design.md.
    """
    archives = sorted(
        source_path(filename, digest)
        for directory in project.package_dirs.values()
        if (manifest := directory / "sources").exists()
        for filename, digest in read_manifest(manifest).items()
    )
    with activity("checking the remote storages"):
        archives = sorted(remote.existing(source, sorted(set(archives) - remote.existing(storage, archives))))
    if archives:
        with activity(f"copying {len(archives)} source archives"):
            remote.transfer(storage, {path: remote.Located(source, path) for path in archives})
    keys, trusted = build_keys(project), source.model_copy(update={"build_keys": storage.build_keys})
    paths = {
        (name, fingerprint): build_path(project, name, keys[name], fingerprint)
        for name in sorted(project.packages)
        for fingerprint in storage.build_keys
    }
    with activity("checking the remote storages"), ThreadPoolExecutor(remote.REQUESTS) as pool:
        present = set(pool.map(lambda p: p if remote.tree_exists(storage, p) else None, paths.values()))
    copied = []
    for (name, fingerprint), path in paths.items():
        if path not in present:
            with activity(f"copying the build of {name}"):
                if remote.copy_tree(trusted, storage, path, signer=fingerprint):
                    copied.append(path)
                    console.print(f"[green]↑[/] {name}")
    if archives or copied:
        with activity("making the pages of the directories"):
            remote_pages.update_pages(
                project, storage, [*(f"{p.rsplit('/', 1)[0]}/" for p in archives), *(f"{p}/" for p in copied)]
            )
    console.print(f"{len(archives)} source archives and {len(copied)} builds copied")


def _push_resolutions(project: Project, storage: Remote, *, signer: Signer | None) -> None:
    """Upload the resolutions this process read, those of the project as it is once loaded, which `storage` misses, or
    has others of: resolved again here, as the versions of the packages of ours they depend on changed, see
    resolution._holds(), which the storage gets in their place, a new generation of their tree.
    """
    caches = project.state / "cache/resolved"
    trees = {remote_tree(cache): cache for cache in sorted(resolutions_read()) if cache.is_relative_to(caches)}

    def stored(tree: str) -> str | None:
        return remote.read_manifest(storage, tree, trusted=signer).get(trees[tree].name)

    with activity("checking the remote storage"), ThreadPoolExecutor(remote.REQUESTS) as pool:
        missing = [t for t, sha in zip(trees, pool.map(stored, trees), strict=True) if sha != sha256_of(trees[t])]
    for tree in missing:
        with activity("uploading the resolutions"):
            remote.upload_tree(storage, tree, trees[tree].parent, [trees[tree].name], signer=signer)
    if missing:
        with activity("making the pages of the directories"):
            remote_pages.update_pages(project, storage, [f"{tree}/" for tree in missing])
    console.print(f"resolutions: {len(missing)} pushed, {len(trees) - len(missing)} already in the remote storage")


def _push_upstream(project: Project, storage: Remote) -> list[str]:
    """Copy the pinned upstream packages the upstream copy of `storage` misses, and return the problems: without the
    lock of the project, like during a build, as it only reads the lock, and writes its own files.
    """
    problems = upstream_copy.push(project, storage)
    for problem in problems:
        console.print(f"[red]✗[/] {escape(problem)}")
    return [f"the upstream copy is incomplete: {len(problems)} problems, see above"] if problems else []


@app.callback(invoke_without_command=True)
def everything(
    ctx: typer.Context,
    final: Final = False,
    arch: Arch = None,
    key: Key = None,
    to: To = "dev",
    from_: Annotated[
        Destination | None,
        typer.Option(
            "--from",
            help="Copy from this bucket instead, server-side, the source archives and the final builds of the "
            "checkout the other one misses: those of a maintenance branch, made in private, for their release.",
        ),
    ] = None,
) -> None:
    """Upload to the remote storage what it does not have yet.

    Without a command: the sources and the builds of all the packages, but the development ones, unless --final, which
    uploads their final builds, with the pages of the directories of what is pushed: xcraft push index makes them all;
    the resolutions of the project, see xcraft push resolutions; then the pinned upstream packages, see xcraft push
    upstream.
    The sources not in the source cache, like those of the packages which did not build, the packages not built, and
    the upstream ones found nowhere, do not keep the others from being pushed, then fail it.
    """
    if ctx.invoked_subcommand:
        # all the options given before the subcommand are its defaults: those given after it win
        params = {k: v for k, v in ctx.params.items() if v is not None and k != "from_"}
        ctx.default_map = {ctx.invoked_subcommand: params}
        return
    project = prepared_project(arch=arch)
    if from_:
        _copy(
            project, remote_storage(project, from_, write=False), remote_storage(project, to, write=from_ != "private")
        )
        return
    storage = remote_storage(project, to)
    absent = _push_sources(project, storage, None)
    signer = key_signer(project, key)
    not_built = _push_builds(project, storage, None, signer=signer, final=final)
    _push_resolutions(project, storage, signer=signer)
    copied = _push_upstream(project, storage)
    if problems := [
        *([f"not in the source cache: {', '.join(absent)}"] if absent else []),
        *([f"not built: {', '.join(not_built)}"] if not_built else []),
        *copied,
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
    project = unprepared_project(packages, arch=arch)
    if absent := _push_sources(project, remote_storage(project, to, write=not check), packages, check=check):
        msg = f"not in the source cache: {', '.join(absent)}"
        raise RuntimeError(msg)


@app.command()
def builds(packages: Packages = None, final: Final = False, arch: Arch = None, key: Key = None, to: To = "dev") -> None:
    """Upload the current builds of the given packages, all by default.

    Development builds are never uploaded: the packages with ones are skipped, unless --final, which uploads their
    final builds.
    """
    project = prepared_project(packages, arch=arch)
    storage, signer = remote_storage(project, to), key_signer(project, key)
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
            signer=None if unsigned else key_signer(project, key),
            upload_signer=key_signer(project, key),
        )


@app.command()
def resolutions(arch: Arch = None, key: Key = None, to: To = "dev") -> None:
    """Upload the resolutions of the project as it is: the images and the build roots of all the architectures, which
    the commands fetch rather than resolving them, like in a new checkout.
    """
    project = prepared_project(arch=arch)
    _push_resolutions(project, remote_storage(project, to), signer=key_signer(project, key))


@app.command("upstream")
def upstream_packages(arch: Arch = None, to: To = "dev") -> None:
    """Copy to the upstream copy of the remote storage the pinned upstream packages it misses, and their source
    packages: from the store of the pinned RPMs of this machine, the latest snapshot, or else their repositories.

    The builds, the resolution and publishing take them from there once their repositories drop them. The lock is
    read as it is, not prepared: its pins may be resolved from that copy only.
    """
    project = Project.find(arch)
    if problems := _push_upstream(project, remote_storage(project, to)):
        raise RuntimeError("; ".join(problems))


@app.command("ccache")
def caches(packages: Packages = None, arch: Arch = None, key: Key = None, to: To = "dev") -> None:
    """Update the compiler caches of the remote storage with those of the given packages, all by default, for CI, with
    the pages of their directories.

    They are not uploaded by xcraft push without a command: they are large, and change with each build.
    """
    project = prepared_project(packages, arch=arch)
    storage = remote_storage(project, to)
    pushed = [
        name
        for name in packages or sorted(project.packages)
        if ccache.push(project, storage, name, signer=key_signer(project, key))
    ]
    for name in pushed:
        console.print(f"[green]↑[/] {name}")
    if pushed:
        with activity("making the pages of the directories"):
            remote_pages.update_pages(project, storage, [f"{ccache.remote_path(project, name)}/" for name in pushed])
    console.print(f"{len(pushed)} pushed")


@app.command()
def index(to: To = "dev") -> None:
    """Make the index.html pages of all the directories of the remote storage, which does not list them."""
    project = Project.find()
    storage = remote_storage(project, to)
    with activity("making the pages of the directories"):
        count = remote_pages.update_pages(project, storage, [""])
    console.print(f"{count} pages")
