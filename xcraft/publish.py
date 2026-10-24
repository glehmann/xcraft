"""Publishing: snapshots of the distribution, made of our builds and the upstream packages they need.

A snapshot covers all the architectures, like the repositories of XCP-ng 8.3: its repository ``base/`` has the
binaries of each architecture, self-contained, in ``base/<arch>/``, and the sources of all of them in
``base/Source/``. It is published as ``repos/<release>/<date>[.<n>]/`` in the remote storage, and copied as
``repos/<release>/latest/``. All snapshots are kept: the storage stores identical files once, and the files already
in the storage are copied there rather than uploaded.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from xcraft import build as builder
from xcraft import remote
from xcraft.build import BuildError, build_path, builds_repo, cached_build
from xcraft.container import DNF_LOCK, run
from xcraft.display import activity, console
from xcraft.localrepo import OUTPUT_MARKER, LocalRepo, extra_rpms, fresh_dir, link
from xcraft.lock import Locked, load_lock, save_lock
from xcraft.resolution import lock_installed, prepare, resolve, resolve_image
from xcraft.signing import Signer, check_signatures
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from xcraft.project import Project, Remote

KEY_FILE = "RPM-GPG-KEY"
"""The public key of the signed snapshots, in them."""
REPO = "base"
"""The repository of our packages, and of the upstream ones they need, in the snapshots."""
BUILD = "build"
"""The repository of the upstream packages the builds of ours need, which the base one does not have."""


def resolve_runtime(project: Project, keys: dict[str, str]) -> list[str]:
    """Add to the lock the upstream packages our builds need, the extra ones and the installer's, and return them.

    They are resolved again when they need upstream source packages not locked yet, which get other versions.
    """
    repo = builds_repo(project, keys)
    for _ in range(3):
        if lock_installed(project, needed := _runtime(project, repo)):
            break
    else:
        msg = "what our packages need at runtime is not available in the versions locked for all the architectures"
        raise BuildError(msg)
    with project.lock():
        lock = load_lock(project)
        lock.runtime[project.arch] = sorted(needed)
        save_lock(project, lock)
    return lock.runtime[project.arch]


def _runtime(project: Project, repo: LocalRepo) -> dict[str, Locked]:
    """Return the upstream packages needed by our packages of `repo`, the extra ones and the installer's."""
    with project.work("runtime") as work:
        (work / "input.json").write_text(
            json.dumps(
                {
                    "locked": {name: [p.evr, p.arch] for name, p in resolve_image(project).packages.items()},
                    "extra": project.config.publish.extra,
                    "installer": project.config.iso.installer,
                }
            )
        )
        run(
            project,
            "runtime",
            f"{DNF_LOCK} python3 /xcraft/runtime.py /work/input.json /work/output.json",
            volumes=[*repo.volumes(), f"{work}:/work"],
            show="resolving what our packages need at runtime",
        )
        output = json.loads((work / "output.json").read_text())
    if output["errors"]:
        for name, error in sorted(output["errors"].items()):
            console.print(f"[red]✗[/] {escape(f'{name}: {error}')}")
        msg = f"cannot install: {', '.join(sorted(output['errors']))}"
        raise BuildError(msg)
    return {name: Locked.model_validate(p) for name, p in output["packages"].items()}


def _download(
    project: Project, files: dict[str, str], on_line: Callable[[str], None], *, source: bool, volumes: list[str]
) -> Path:
    """Download the upstream packages `files`, as file name: NEVRA, to their cache, and return it.

    Those not in the cache come from the latest snapshot, or from the repositories, which may not have them
    anymore, after an update, while the storage may not be there. The signatures of the source packages are
    checked before the cache, which would keep a bad one.
    """
    cache = project.state / ("cache/srpms" if source else "cache/rpms")
    if not (missing := {f: nevra for f, nevra in files.items() if not (cache / f).exists()}):
        return cache
    with project.work("download") as work:
        if storage := project.config.remote:
            with contextlib.suppress(remote.RemoteError):
                for f in missing:
                    on_line(f)
                    for repo in (REPO, BUILD):  # where it is, if anywhere
                        path = f"{_snapshot_path(project, 'latest')}/{repo}/{'Source' if source else project.arch}/{f}"
                        if remote.download(storage, path, work / f):
                            break
        if left := sorted({nevra for f, nevra in missing.items() if not (work / f).exists()}):
            run(
                project,
                "download",
                # as root, for the lock: the files are given back to the user
                f"{DNF_LOCK} dnf -q download {'--source ' if source else ''}--destdir /work {' '.join(left)}"
                " && sudo chown builder: /work/*.rpm",
                volumes=[f"{work}:/work", *volumes],
                on_line=on_line,
            )
        if source:
            check_signatures(project, work, list(missing), what="upstream source packages")
        cache.mkdir(parents=True, exist_ok=True)
        for f in missing:
            (work / f).rename(cache / f)
    return cache


def _nevra(packages: dict[str, Locked], name: str) -> str:
    return f"{name}-{packages[name].evr}.{packages[name].arch}"


def _upstream_rpms(project: Project, names: list[str], on_line: Callable[[str], None]) -> list[Path]:
    """Return the RPMs of locked upstream packages: from the cache, the latest snapshot, or the repositories."""
    locked = resolve_image(project).packages
    rpms = {name: f"{name}-{locked[name].evr.split(':')[-1]}.{locked[name].arch}.rpm" for name in names}
    cache = _download(project, {rpms[name]: _nevra(locked, name) for name in names}, on_line, source=False, volumes=[])
    for name in names:
        if sha256_of(cache / rpms[name]) != locked[name].sha256:
            (cache / rpms[name]).unlink()
            msg = f"{rpms[name]} does not match the sha256 of the lock"
            raise BuildError(msg)
    return [cache / rpm for rpm in rpms.values()]


def _upstream_srpms(project: Project, names: list[str], on_line: Callable[[str], None]) -> list[Path]:
    """Return the source RPMs of locked upstream packages: from the cache, the latest snapshot, or the repositories.

    They are those of the packages' metadata: binaries may not have the versions of their sources.
    """
    if not names:  # dnf repoquery would list everything
        return []
    locked = resolve_image(project).packages
    nevras = {name: _nevra(locked, name) for name in names}
    with project.work("srpms") as work:
        # only for these downloads, not to change the build environment
        (work / "sources.repo").write_text(
            "".join(
                f"[{name}-source]\nname={name} sources\nbaseurl={repo.source_baseurl}\nenabled=0\ngpgcheck=0\n"
                for name, repo in project.repos.items()
                if repo.source_baseurl
            )
        )
        volumes = [f"{work / 'sources.repo'}:/etc/yum.repos.d/xcraft-sources.repo:ro"]
        run(
            project,
            "srpms",
            f"{DNF_LOCK} dnf -q repoquery --qf '%{{name}} %{{sourcerpm}}' {' '.join(nevras.values())} > /work/srpms",
            volumes=[f"{work}:/work", *volumes],
        )
        srpms = {
            n: srpm
            for n, srpm in (line.split() for line in (work / "srpms").read_text().splitlines() if line)
            if n in nevras
        }
        if unknown := sorted(set(names) - srpms.keys()):
            msg = f"no source package for: {', '.join(unknown)}"
            raise BuildError(msg)
        files = {srpm: nevras[name] for name, srpm in srpms.items()}
        cache = _download(project, files, on_line, source=True, volumes=volumes)
    return [cache / srpm for srpm in sorted(files)]


def _signed_path(project: Project, signer: Signer, name: str, key: str) -> str:
    """Return the path of the tree of a signed build in the remote storage."""
    return f"signed/{signer.key}/{project.arch}/{name}/{key}"


def _signed_build(project: Project, signer: Signer, name: str, key: str, *, push: bool) -> Path:
    """Return the build of `name` with `key`, signed by `signer`: from the cache, the remote storage, or signed now.

    A build is signed once: the signatures have a date, so signing again would not give the same files, which the
    storage could not store once for all the snapshots. With `push`, the signed build goes to the remote storage
    when it does not have it: signed now, or by a local publish before.
    """
    signed = project.state / "cache/signed" / signer.key / name / key
    storage, path = project.config.remote, _signed_path(project, signer, name, key)
    if not signed.exists() and not (storage and remote.download_tree(storage, path, signed)):
        work = signed.with_name(f".{key}")
        shutil.rmtree(work, ignore_errors=True)
        for files in ("RPMS", "SRPMS"):
            shutil.copytree(cached_build(project, name, key) / files, work / files)  # copies: signing changes them
        signer.sign_rpms(project, work)
        work.rename(signed)
    if push and storage and not remote.tree_exists(storage, path):
        remote.upload_tree(storage, path, signed, ["RPMS", "SRPMS"])
    return signed


def assemble(
    platforms: list[tuple[Project, dict[str, str]]], dest: Path, *, signer: Signer | None, push: bool, build: bool
) -> None:
    """Make `dest` a snapshot of the `platforms`, with the builds of our packages with their keys.

    Each binary must be installable alone, from the binaries of its architecture. Our source packages are those of
    the first platform: those of the others, built from the same sources, differ by their build times. With `build`,
    the snapshot has the build repository too, whose binaries are installable from it and the base one. With a
    `signer`, our packages and the repositories' metadata are signed, and the public key is in `dest`.
    """
    fresh_dir(platforms[0][0], dest)
    sources = {
        repo: LocalRepo(platforms[0][0], dest / repo / "Source") for repo in (REPO, BUILD) if build or repo == REPO
    }
    for repo in sources.values():
        repo.reset(extra_upstream=False)
    for project, keys in platforms:
        _assemble_arch(project, keys, dest, sources[REPO], signer=signer, push=push)
        if build:
            _assemble_build(project, dest, sources[BUILD], signer=signer)
    for repo in sources.values():
        repo.index()
        if signer:
            signer.sign_file(repo.path / "repodata/repomd.xml")
    if signer:
        (dest / KEY_FILE).write_text(signer.public_key())


def _assemble_arch(  # noqa: PLR0913
    project: Project, keys: dict[str, str], dest: Path, sources: LocalRepo, *, signer: Signer | None, push: bool
) -> None:
    """Make the repository of the binaries of the platform of `project` in `dest`, adding their sources to `sources`."""
    binaries = LocalRepo(project, dest / REPO / project.arch)
    binaries.reset(extra_upstream=False)
    arch, source = f"{REPO}/{project.arch}", f"{REPO}/Source"
    ours = []
    with activity(f"signing our packages for {project.arch}" if signer else "getting our packages") as on_line:
        for name in sorted(keys):
            on_line(name)
            build = (
                _signed_build(project, signer, name, keys[name], push=push)
                if signer
                else cached_build(project, name, keys[name])
            )
            binaries.add(build)
            ours += [f"{arch}/{rpm.name}" for rpm in (build / "RPMS").rglob("*.rpm")]
            for srpm in (build / "SRPMS").glob("*.src.rpm"):
                if not (sources.path / srpm.name).exists():
                    link(srpm, sources.path / srpm.name)
                    ours.append(f"{source}/{srpm.name}")
    if signer:
        check_signatures(project, dest, ours, what="packages of ours", public_key=signer.public_key())
    # neither ours nor from the repositories: as they are
    for rpm in extra_rpms(project):
        link(rpm, binaries.path / rpm.name)
    _add_upstream(project, load_lock(project).runtime.get(project.arch, []), dest, binaries, sources)
    binaries.index()
    if signer:
        signer.sign_file(binaries.path / "repodata/repomd.xml")
    _check_installable(project, [binaries.path])


def build_dependencies(project: Project) -> list[str]:
    """Return the upstream packages the builds of ours need, but those of the base repository.

    Those are the packages of the image of the build environment and of the build roots, but the upstream ones ours
    replace, like kernel-headers.
    """
    resolved, runtime = resolve(project), load_lock(project).runtime.get(project.arch, [])
    ours = {binary.name for package in project.packages.values() for binary in package.binaries}
    return sorted({*resolved.image, *(n for names in resolved.buildroots.values() for n in names)} - ours - {*runtime})


def _assemble_build(project: Project, dest: Path, sources: LocalRepo, *, signer: Signer | None) -> None:
    """Make the build repository of the platform of `project` in `dest`, adding the sources to `sources`."""
    binaries = LocalRepo(project, dest / BUILD / project.arch)
    binaries.reset(extra_upstream=False)
    _add_upstream(project, build_dependencies(project), dest, binaries, sources)
    binaries.index()
    if signer:
        signer.sign_file(binaries.path / "repodata/repomd.xml")
    _check_installable(project, [dest / REPO / project.arch, binaries.path])


def _add_upstream(project: Project, names: list[str], dest: Path, binaries: LocalRepo, sources: LocalRepo) -> None:
    """Add the upstream packages `names` to `binaries`, and their sources to `sources`, checking their signatures."""
    with activity(f"getting the upstream packages for {binaries.path.relative_to(dest)}") as on_line:
        rpms = _upstream_rpms(project, names, on_line)
        for rpm in rpms:
            link(rpm, binaries.path / rpm.name)
    added = [(binaries.path / rpm.name).relative_to(dest).as_posix() for rpm in rpms]
    with activity(f"getting their sources, for {sources.path.relative_to(dest)}") as on_line:
        for srpm in _upstream_srpms(project, names, on_line):
            if not (sources.path / srpm.name).exists():  # the source of several binaries, or architectures
                link(srpm, sources.path / srpm.name)
                added.append((sources.path / srpm.name).relative_to(dest).as_posix())
    check_signatures(project, dest, added, what="upstream packages")


def _check_installable(project: Project, repos: list[Path]) -> None:
    """Check each package of the last repository of `repos` can be installed from them alone."""
    with project.work("snapshot") as work:
        run(
            project,
            "snapshot",
            f"{DNF_LOCK} python3 /xcraft/installable.py /work/errors.json "
            + " ".join(f"/repos/{i}" for i in range(len(repos))),
            volumes=[*(f"{repo}:/repos/{i}:ro" for i, repo in enumerate(repos)), f"{work}:/work"],
            show=f"checking that {repos[-1].parent.name}/{repos[-1].name} is self-contained",
        )
        errors = json.loads((work / "errors.json").read_text())
    if errors:
        for package, error in sorted(errors.items()):
            console.print(f"[red]✗[/] {escape(f'{package}: {error}')}")
        msg = f"the snapshot is not self-contained: {len(errors)} packages cannot be installed from it"
        raise BuildError(msg)


def _snapshot_path(project: Project, name: str) -> str:
    """Return the path of a snapshot in the storage, with its repositories."""
    return f"repos/{project.config.publish.release}/{name}"


def snapshot_name(project: Project, storage: Remote) -> str:
    """Return the name of a new snapshot: today's date, with the first suffix not used."""
    day = datetime.now(UTC).date().isoformat()
    for name in (day, *(f"{day}.{n}" for n in range(2, 1000))):
        if not remote.exists(storage, f"{_snapshot_path(project, name)}/{REPO}/Source/repodata/repomd.xml"):
            return name
    msg = f"too many snapshots on {day}"
    raise BuildError(msg)


def _delete_stale(storage: Remote, prefix: str, keep: set[str]) -> None:
    """Delete the files under `prefix` but those of `keep`, and the pages of the directories, written again after."""
    stale = [p for p in remote.listing(storage, f"{prefix}/") if p.removeprefix(f"{prefix}/") not in keep]
    remote.delete(storage, [p for p in stale if Path(p).name != remote.INDEX])


def upload_snapshot(
    platforms: list[tuple[Project, dict[str, str]]], src: Path, name: str, *, signer: Signer | None
) -> None:
    """Upload the snapshot in `src` of the `platforms` as `name`, and make it the latest one.

    The packages already in the storage, in the latest snapshot or the builds with the keys of the platforms, are
    copied there.
    """
    project = platforms[0][0]
    storage = project.config.remote
    if not storage:
        msg = "no remote storage: see [remote] in xcraft.toml"
        raise BuildError(msg)
    snapshot, latest = _snapshot_path(project, name), _snapshot_path(project, "latest")
    files = sorted(f.relative_to(src).as_posix() for f in src.rglob("*") if f.is_file() and f.name != OUTPUT_MARKER)
    manifest = {f: sha256_of(src / f) for f in files}
    # where the storage has the same files, by SHA-256: the latest snapshot, and the trees of the builds
    stored = {
        f: f"{latest}/{f}" for f, digest in remote.read_manifest(storage, latest).items() if manifest.get(f) == digest
    }
    for platform, keys in platforms:
        for package, key in keys.items():
            tree = _signed_path(platform, signer, package, key) if signer else build_path(platform, package, key)
            for path, digest in remote.read_manifest(storage, tree).items():
                repo = {"RPMS": f"{REPO}/{platform.arch}", "SRPMS": f"{REPO}/Source"}.get(path.split("/")[0])
                if repo and manifest.get(f := f"{repo}/{Path(path).name}") == digest:
                    stored[f] = f"{tree}/{path}"
    # the packages before the metadata listing them, and repomd.xml with its signature, as close as can be
    last = [f for f in files if f.endswith(("/repomd.xml", "/repomd.xml.asc"))]
    order = [f for f in files if f.endswith(".rpm")] + [f for f in files if not f.endswith(".rpm") and f not in last]
    with activity(f"publishing {name}") as on_line:
        for file in [*order, *last]:
            on_line(file)
            if file in stored:
                remote.copy(storage, stored[file], f"{snapshot}/{file}")
            else:
                remote.upload(storage, f"{snapshot}/{file}", src / file)
        remote.write_manifest(storage, snapshot, manifest)
        # the files of a failed publish of the same name, and of the previous latest snapshot, but the pages
        keep = {*files, remote.MANIFEST}
        _delete_stale(storage, snapshot, keep)
        on_line("latest")
        for file in [*order, *last]:
            remote.copy(storage, f"{snapshot}/{file}", f"{latest}/{file}")
        remote.write_manifest(storage, latest, manifest)
        _delete_stale(storage, latest, keep)
        on_line("the pages of the directories")
        signed = [f"signed/{signer.key}/{platform.arch}/" for platform, _ in platforms] if signer else []
        remote.update_pages(storage, [f"{snapshot}/", f"{latest}/", *signed])
    console.print(f"[bold green]✓[/] published {snapshot}, and as {latest}")


def snapshot(
    platforms: list[Project], dest: Path, *, signer: Signer | None, push: bool, build: bool
) -> list[tuple[Project, dict[str, str]]]:
    """Make the snapshot of the `platforms` in `dest`, and return them with the keys of their packages.

    Our packages are built or fetched first, but only fetched for the platforms the host emulates. With `build`, the
    snapshot has the build repository too.
    """
    built, upstream = [], {}
    for project in platforms:
        prepare(project)
        results = builder.build(project, sorted(project.packages), build_missing=project.native)
        if failed := [r.name for r in results if r.failed]:
            msg = f"not built for {project.arch}: {', '.join(failed)}"
            raise RuntimeError(msg)
        keys = builder.build_keys(project)
        upstream[project.arch] = f"{len(resolve_runtime(project, keys))} upstream ones" + (
            f", and {len(build_dependencies(project))} to build them" if build else ""
        )
        built.append((project, keys))
    assemble(built, dest, signer=signer, push=push, build=build)
    console.print(
        f"[bold green]✓[/] snapshot in {dest}: "
        + ", ".join(f"{p.arch}: {len(keys)} packages, {upstream[p.arch]}" for p, keys in built)
    )
    return built
