"""xcraft publish."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

import typer
from pydantic import ValidationError

from xcraft import contents, remote
from xcraft import publish as publisher
from xcraft.build import update_build_ids
from xcraft.cache import build_keys, cached_build, release_suffix
from xcraft.commands import Key, To, choose_signer, complete_arch, prepared_project, remote_storage, sign_builds
from xcraft.display import console
from xcraft.iso import check_iso_config, fetch_isos, push_isos, snapshot_isos
from xcraft.project import Destination, Project, Publish, Remote
from xcraft.publish import ISO_DIR, REPO, snapshot, snapshot_tree
from xcraft.signing import KEY_FILE, dev_signer, new_dev_key, repo_keys
from xcraft.upload import Uploader

app = typer.Typer()

Locked = Annotated[bool, typer.Option("--locked", help="Fail when publishing would change the lock, for CI.")]
Local = Annotated[
    bool, typer.Option("--local", help="Only make the snapshot locally, in --output, without publishing it.")
]
Output = Annotated[Path | None, typer.Option(help="Directory of the local snapshot. .xcraft/publish/ by default.")]
PublishDev = Annotated[
    str | None,
    typer.Option(
        "--dev",
        metavar="ID",
        help="Publish in dev mode, like the CI of a pull request: with the development builds of ID, see xcraft build "
        "--dev, which are files of the stage only, never pushed as builds, and the final builds and upstream packages "
        "of the main bucket copied from it, all keeping the signatures of their builds, whose keys the stage has.",
        show_default=False,
    ),
]
DevKey = Annotated[bool, typer.Option("--dev-key", help="Sign with the developer's key, for a test storage.")]
Stage = Annotated[
    str | None,
    typer.Option("--stage", help="Stage subdirectory for repositories and ISOs. Defaults to the current user name."),
]
Summary = Annotated[
    Path | None,
    typer.Option(
        help="Write in this file, as Markdown, where the snapshot is published, how a host uses its repositories, and "
        "its development builds, like for the comment of a pull request.",
        show_default=False,
    ),
]
Arch = Annotated[
    str | None,
    typer.Option(
        help="RPM architecture to publish, like aarch64, or all. By default, all of them, but for a --local "
        "snapshot, the one of $XCRAFT_ARCH, or the host's.",
        show_default=False,
        autocompletion=lambda incomplete: [a for a in [*complete_arch(incomplete), "all"] if a.startswith(incomplete)],
    ),
]


def _iso_platforms(project: Project, platforms: list[Project], only: str | None) -> list[Project]:
    """Return the `platforms` of the ISOs to publish, unless `only` the repos: those of x86_64, for their boot loaders,
    when [iso] has the installer's packages.
    """
    if only == "isos" and not project.config.iso.installer:
        msg = "no ISO to publish: see [iso] in xcraft.toml"
        raise typer.BadParameter(msg)
    if only == "repos" or not project.config.iso.installer:
        return []
    if not (found := [p for p in platforms if p.arch.startswith("x86_64")]) and only == "isos":
        msg = f"the ISOs are for x86_64, not {', '.join(p.arch for p in platforms)}"
        raise typer.BadParameter(msg)
    for platform in found:  # before the snapshot
        check_iso_config(platform)
    return found


def _repo_file(project: Project) -> str:
    return f"xcraft-{project.config.publish.stage}-{project.arch}.repo"


def _repo_files(
    platforms: list[Project], storage: Remote, name: str, key_files: list[str], *, keep: bool
) -> dict[str, str]:
    """Return the .repo file of each of the `platforms` of the snapshot `name`, by file name, for the hosts: its
    repository, trusting the keys of `key_files`, in it, and with `keep`, those of the repositories of xcraft.toml,
    whose packages keep their signatures.
    """
    project = platforms[0]
    tree = f"{storage.url.rstrip('/')}/{snapshot_tree(project, 'repos', name)}"
    keys = [f"{tree}/{f}" for f in key_files] + (repo_keys(project) if keep else [])
    release, stage = project.config.publish.release, project.config.publish.stage
    return {
        _repo_file(platform): (
            f"[xcraft-{stage}]\nname={release} {stage} {name}\nbaseurl={tree}/{REPO}/{platform.arch}/\nenabled=1\n"
            f"gpgcheck=1\nrepo_gpgcheck=1\ngpgkey={' '.join(keys)}\nmetadata_expire=0\n"
        )
        for platform in platforms
    }


def _key_files(files: list[str]) -> list[str]:
    """Return the public keys of a snapshot of `files`, by path in it."""
    return sorted(f for f in files if "/" not in f and f.startswith(KEY_FILE))


def _write_snapshot_files(
    built: list[tuple[Project, dict[str, str]]], dest: Path, storage: Remote, name: str, *, keep: bool
) -> None:
    """Write in the snapshot `dest`, to publish as `name`, its contents, of the platforms `built`, with the keys of
    their builds, see xcraft contents, and the .repo file of each platform, see _repo_files(). The latest snapshot of
    the stage has the copy of those of the last one.
    """
    published = contents.Published(arches={p.arch: contents.contents(p, keys) for p, keys in built})
    (dest / contents.FILE).write_text(published.model_dump_json())
    keys = _key_files([f.name for f in dest.iterdir()])
    for file, text in _repo_files([p for p, _ in built], storage, name, keys, keep=keep).items():
        (dest / file).write_text(text)


def _write_summary(
    path: Path,
    project: Project,
    built: list[tuple[Project, dict[str, str]]],
    isos: list[str],
    storage: Remote,
    name: str,
) -> None:
    """Write in `path`, as Markdown, where the snapshot `name` is published: the repositories of the platforms
    `built`, if any, and its ISOs, by file name, how a host uses the repositories, and the files of the development
    builds, with their links.
    """
    url = storage.url.rstrip("/")

    def link(tree: str, file: str) -> str:
        return f"[{file}]({url}/{quote(f'{tree}/{file}')})"

    lines = []
    if built:
        lines += [f"- Repositories: {url}/{snapshot_tree(project, 'repos', name)}/"]
    if isos:
        tree = snapshot_tree(project, "isos", name)
        lines += [f"- ISO: {link(tree, f)}" for f in sorted(isos)]
    for platform, keys in built:
        tree = snapshot_tree(platform, "repos", name)
        lines += [
            "",
            f"On the {platform.arch} hosts, to install its packages:",
            "```sh",
            f"dnf config-manager --add-repo {url}/{tree}/{_repo_file(platform)}",
            "dnf upgrade --refresh",
            "```",
        ]
        if dev := {n: cached_build(platform, n, k) for n, k in sorted(keys.items()) if "~" in release_suffix(k)}:
            lines += ["", f"Development builds, {platform.arch}, by source package:", ""]
        for package, build in dev.items():
            files = [(f"{REPO}/{platform.arch}", f.name) for f in sorted(build.glob("RPMS/*/*.rpm"))]
            files += [(f"{REPO}/Source", f.name) for f in sorted(build.glob("SRPMS/*.rpm"))]
            lines += [f"- {package}", *(f"  - {link(f'{tree}/{repo}', f)}" for repo, f in files)]
    path.write_text("\n".join(lines) + "\n")


def _reused(
    uploader: Uploader, platforms: list[Project], only: str | None, isos: list[Project], summary: Path | None
) -> bool:
    """Publish the latest snapshot again, with its own .repo files, with a `summary`, when it is made of the same, see
    Uploader.reuse(), and return whether it was.
    """
    project, storage = platforms[0], uploader.storage

    def files(name: str, manifest: dict[str, str]) -> dict[str, str]:
        return _repo_files(platforms, storage, name, _key_files(list(manifest)), keep=False)

    if not (name := uploader.reuse(platforms, only=only, isos=isos, repo_files=files)):
        return False
    if summary:
        trusted = publisher.trusted_key(project, uploader.signer)
        manifest = remote.read_manifest(storage, snapshot_tree(project, "isos", name), trusted=trusted) if isos else {}
        built = [] if only == "isos" else [(p, build_keys(p)) for p in platforms]
        _write_summary(Path(summary), project, built, [f for f in manifest if f.endswith(".iso")], storage, name)
    return True


def _publish(  # noqa: C901, PLR0912
    only: str | None,
    *,
    locked: bool,
    local: bool,
    output: Path | None,
    dev_key: bool,
    key: str | None,
    to: Destination,
    stage: str | None,
    arch: str | None,
    dev: str | None,
    summary: Path | None,
) -> None:
    """Publish the repositories and the ISOs of a snapshot, or `only` the repos or the isos, of dev mode with `dev`,
    with a `summary` of where it is published.
    """
    if dev is not None and not re.fullmatch(r"[a-zA-Z0-9]{1,16}", dev):
        msg = f"the development build ID must be 1 to 16 letters and digits: {dev}"
        raise typer.BadParameter(msg, param_hint="--dev")
    project = prepared_project(locked=locked, arch=None if arch == "all" else arch)
    if stage is not None:
        try:
            publish = Publish.model_validate(project.config.publish.model_dump() | {"stage": stage})
        except ValidationError as e:
            msg = "stage must start with a letter, digit or underscore and contain only letters, digits, _, - or ."
            raise typer.BadParameter(msg, param_hint="--stage") from e
        project = replace(project, config=project.config.model_copy(update={"publish": publish}))
    # signed always: by --key, [sign] or the developer's key, which a local snapshot, or one of the dev bucket, makes
    # when there is none
    if not (local or dev_key or key or to != "dev" or dev_signer(project)):
        new_dev_key(project)
    sign_builds(
        project,
        key,
        final=False,
        signer=choose_signer(
            project, local=local, unsigned=False, dev_key=dev_key or (not local and to == "dev" and not key), key=key
        ),
    )  # before the platforms, which copy it
    signer = project.signer
    platforms = project.all_platforms() if arch == "all" or (arch is None and not local) else [project]
    if arch is None and len(platforms) < len(others := project.all_platforms()):
        console.print(
            f"[yellow]warning[/]: only {project.arch} is published, not "
            f"{', '.join(p.arch for p in others if p.arch != project.arch)}: --arch all publishes them all"
        )
    storage = None if local else remote_storage(project, to)
    if not project.config.publish.release:
        msg = "no release to publish: see [publish] in xcraft.toml"
        raise typer.BadParameter(msg)
    with_isos = _iso_platforms(project, platforms, only)
    with project.lock():
        update_build_ids(project)  # the final builds are numbered by them
        # of ctx.params, which typer does not convert
        dest = (Path(output) if output else project.state / "publish").absolute()
        build = only != "isos" and project.config.publish.build
        uploader = Uploader(project, storage, signer, build, dev=dev is not None) if storage else None
        # the latest snapshot reused, but in dev mode: each run makes its development builds
        if uploader and output is None and dev is None and _reused(uploader, platforms, only, with_isos, summary):
            return
        console.print("assembling the snapshot")
        built, stored, checkout = snapshot(
            platforms, dest, signer=signer, storage=storage, build=build, dev=dev, keep=dev is not None
        )
        if storage and output is not None:  # all of it, in the directory asked
            for repo in sorted({path.rsplit("/", 1)[0] for path in stored}):
                publisher.materialize(storage, dest, stored, repo)
        for platform in with_isos:
            console.print(f"getting the ISOs for {platform.arch}")
            # made from its packages
            if storage and stored and (dev is not None or not fetch_isos(platform, signer=signer)):
                publisher.materialize(storage, dest, stored, f"{REPO}/{platform.arch}")
            repo = dest / REPO / platform.arch
            for iso in snapshot_isos(platform, repo, checkout, dest / ISO_DIR, signer=signer, dev=dev):
                console.print(f"[bold green]✓[/] {iso}")
        if uploader:
            publisher.check_checkout(project, checkout)  # which the keys of what is uploaded are computed from
            isos = {
                file: source
                for platform in with_isos
                if dev is None  # those of the checkout: of final builds
                for file, source in push_isos(platform, uploader.storage, signer=signer).items()
            }
            name = uploader.snapshot_name()
            if only != "isos":
                _write_snapshot_files(built, dest, uploader.storage, name, keep=dev is not None)
                uploader.upload_snapshot(built, dest, name, stored=stored)
            if with_isos:
                uploader.upload_isos(dest, name, stored=isos)
            if summary:
                _write_summary(
                    Path(summary),
                    project,
                    [] if only == "isos" else built,
                    [f.name for f in (dest / ISO_DIR).glob("*.iso")] if with_isos else [],
                    uploader.storage,
                    name,
                )


@app.callback(invoke_without_command=True)
def everything(
    ctx: typer.Context,
    locked: Locked = False,
    local: Local = False,
    output: Output = None,
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
    dev: PublishDev = None,
    summary: Summary = None,
) -> None:
    r"""Publish a snapshot of the distribution in the remote storage, as the latest one, for all the architectures.

    Without a command: its repositories, and its ISOs, when \[iso] in xcraft.toml has the installer's packages. It has
    all our packages, built or fetched first, and the upstream packages they need at runtime, and the extra ones of
    xcraft.toml, as locked, and it must be self-contained. Those of the architectures the host emulates are only
    fetched, built by native builders.
    """
    if ctx.invoked_subcommand:
        # all the options given before the subcommand are its defaults: those given after it win
        ctx.default_map = {ctx.invoked_subcommand: {k: v for k, v in ctx.params.items() if v is not None}}
        return
    _publish(None, **ctx.params)


@app.command()
def repos(
    ctx: typer.Context,
    locked: Locked = False,
    local: Local = False,
    output: Output = None,
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
    dev: PublishDev = None,
    summary: Summary = None,
) -> None:
    """Publish repositories as repos/<release>/<stage>/<date>/, and as the stage's latest snapshot."""
    _publish("repos", **ctx.params)


@app.command()
def isos(
    ctx: typer.Context,
    locked: Locked = False,
    local: Local = False,
    output: Output = None,
    dev_key: DevKey = False,
    key: Key = None,
    to: To = "dev",
    stage: Stage = None,
    arch: Arch = None,
    dev: PublishDev = None,
    summary: Summary = None,
) -> None:
    """Publish signed packages and netinstall ISOs as isos/<release>/<stage>/<date>/, and as the stage's latest ones.

    They are made for the x86_64 architectures, like xcraft iso, in the ISO directory of the snapshot.
    """
    _publish("isos", **ctx.params)
