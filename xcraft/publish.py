"""Publishing: snapshots of the distribution, made of our builds and the upstream packages they need.

A snapshot covers all the architectures, like the repositories of XCP-ng 8.3: its repository ``base/`` has the
binaries of each architecture, self-contained, in ``base/<arch>/``, and the sources of all of them in
``base/Source/``. It is published as ``repos/<release>/<stage>/<date>[.<n>]/`` in the remote storage, and copied as
``repos/<release>/<stage>/latest/``. All snapshots are kept: the storage stores identical files once, and files
already in the storage are copied there rather than uploaded.
"""

from __future__ import annotations

import contextlib
import hashlib
import inspect
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from rich.markup import escape

from xcraft import build as builder
from xcraft import remote, repodata
from xcraft.cache import BuildError, build_keys, build_path, builds_repo, cached_build
from xcraft.container import BUILDENV, DNF_LOCK, image_tag, run
from xcraft.display import activity, console
from xcraft.localrepo import OUTPUT_MARKER, LocalRepo, extra_rpms, fresh_dir, link
from xcraft.lock import Locked, load_lock, save_lock
from xcraft.resolution import lock_installed, prepare, resolve, resolve_image
from xcraft.signing import Signer, check_signatures, dev_signer, signing_key
from xcraft.sources import sha256_of
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from xcraft.build import Fetch
    from xcraft.project import Project, Remote
    from xcraft.repodata import Entry

KEY_FILE = "RPM-GPG-KEY"
"""The public key of the signed snapshots, in them."""
REPO = "base"
"""The repository of our packages, and of the upstream ones they need, in the snapshots."""
BUILD = "build"
"""The repository of the upstream packages the builds of ours need, which the base one does not have."""
ISO_DIR = "iso"
"""The ISOs in a local snapshot, by architecture, published apart from its repositories."""
INPUTS = "xcraft-inputs.json"
"""Checkout input keys, covered by the snapshot's signed manifest."""


class SnapshotInputs(BaseModel):
    """Input keys of the architectures or ISO variants of a published snapshot."""

    keys: dict[str, str]


def snapshot_key(project: Project, keys: dict[str, str], *, signer: Signer | None, build: bool) -> str:
    """Identify a snapshot without fetching its RPMs or building containers."""
    lock = load_lock(project)
    data = [
        project.platform, project.config.platforms, keys, lock.sources, lock.runtime.get(project.arch, []),
        project.config.publish.extra, project.config.iso.installer, project.config.extra_upstream,
        {n: repo.model_dump() for n, repo in project.repos.items()},
        {n: p.model_dump() for n, p in resolve_image(project).packages.items()},
        signer.key if signer else None, build, hash_dir(BUILDENV),
        [inspect.getsource(f) for f in
         (assemble, _assemble_arch, _assemble_build, _add_upstream, _upstream_rpms, _upstream_srpms, _srpm_names,
          _signed_build, _runtime, _check_installable, LocalRepo, remote_snapshot, _Plan, _add_ours,
          _add_stored_upstream, repodata)],
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _reusable(
    project: Project, storage: Remote, kind: str, keys: dict[str, str], signer: Signer | None
) -> dict[str, str]:
    prefix = _tree(project, kind, "latest")
    manifest = remote.read_manifest(storage, prefix, trusted=signer or dev_signer(project))
    if INPUTS not in manifest:
        return {}
    with project.work("snapshot-inputs") as work:
        path = work / INPUTS
        if not remote.download(storage, f"{prefix}/{INPUTS}", path) or sha256_of(path) != manifest[INPUTS]:
            return {}
        try:
            return manifest if SnapshotInputs.model_validate_json(path.read_bytes()).keys == keys else {}
        except ValueError:
            return {}


def reuse_published(  # noqa: PLR0913
    platforms: list[Project],
    storage: Remote,
    *,
    signer: Signer | None,
    build: bool,
    only: str | None,
    isos: list[Project],
) -> bool:
    """Republish matching trusted snapshots by server-side copies, without reconstructing their payloads."""
    from xcraft.iso import iso_inputs  # noqa: PLC0415, iso imports snapshot assembly

    project = platforms[0]
    wanted = {}
    if only != "isos":
        wanted["repos"] = {p.arch: snapshot_key(p, build_keys(p), signer=signer, build=build) for p in platforms}
    if isos:
        wanted["isos"] = {name: key for p in isos for name, key in iso_inputs(p, signer=signer).items()}
    manifests = {kind: _reusable(project, storage, kind, keys, signer) for kind, keys in wanted.items()}
    if not manifests or not all(manifests.values()):
        return False
    name = snapshot_name(project, storage)
    for kind, manifest in manifests.items():
        latest, target = _tree(project, kind, "latest"), _tree(project, kind, name)
        with activity(f"reusing the published {kind} as {target}", log=True) as on_line:
            for files in _batches(list(manifest)):
                remote.transfer(storage, {f"{target}/{f}": f"{latest}/{f}" for f in files}, on_line)
            remote.write_manifest(storage, target, manifest, signer=signer or signing_key(project))
            _delete_stale(storage, target, {*manifest, remote.MANIFEST, remote.SIGNATURE})
            remote.update_pages(project, storage, [f"{target}/", f"{latest}/"])
        console.print(f"[bold green]✓[/] published {target}, and as {latest} (reused the matching snapshot)")
    return True


def resolve_runtime(project: Project, repo: LocalRepo) -> list[str]:
    """Add to the lock the upstream packages our builds of `repo` need, the extra ones and the installer's, and
    return them.

    They are resolved again when they need upstream source packages not locked yet, which get other versions.
    """  # noqa: D205
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
        if storage := project.config.remote.get("main"):
            latest = _tree(project, "repos", "latest")

            def fetch(f: str) -> None:
                for repo in (REPO, BUILD):  # where it is, if anywhere
                    if remote.download(
                        storage, f"{latest}/{repo}/{'Source' if source else project.arch}/{f}", work / f
                    ):
                        on_line(f)
                        return

            with contextlib.suppress(remote.RemoteError), ThreadPoolExecutor(remote.REQUESTS) as pool:
                list(pool.map(fetch, missing))
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
    if not names:
        return []
    locked = resolve_image(project).packages
    with _sources(project) as (work, volumes):
        files = {srpm: _nevra(locked, name) for name, srpm in _srpm_names(project, names, work, volumes).items()}
        cache = _download(project, files, on_line, source=True, volumes=volumes)
    return [cache / srpm for srpm in sorted(files)]


@contextlib.contextmanager
def _sources(project: Project) -> Generator[tuple[Path, list[str]]]:
    """Return a work directory, and the volumes of the repositories of the upstream source packages."""
    with project.work("srpms") as work:
        # only for these downloads, not to change the build environment
        (work / "sources.repo").write_text(
            "".join(
                f"[{name}-source]\nname={name} sources\nbaseurl={repo.source_baseurl}\nenabled=0\ngpgcheck=0\n"
                for name, repo in project.repos.items()
                if repo.source_baseurl
            )
        )
        yield work, [f"{work / 'sources.repo'}:/etc/yum.repos.d/xcraft-sources.repo:ro"]


def _srpm_names(project: Project, names: list[str], work: Path, volumes: list[str]) -> dict[str, str]:
    """Return the source packages of the locked upstream packages `names`, as in the metadata of the repositories."""
    if not names:  # dnf repoquery would list everything
        return {}
    locked = resolve_image(project).packages
    nevras = {name: _nevra(locked, name) for name in names}
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
    return srpms


def _signed_path(project: Project, signer: Signer, name: str, key: str) -> str:
    """Return the path of the tree of a signed build in the remote storage."""
    return f"signed/{signer.key}/{project.arch}/{name}/{key}"


def _signed_build(project: Project, signer: Signer, name: str, key: str, *, storage: Remote | None) -> Path:
    """Return the build of `name` with `key`, signed by `signer`: from the cache, the remote storage, or signed now.

    A build is signed once: the signatures have a date, so signing again would not give the same files, which the
    storage could not store once for all the snapshots. With `storage`, the signed build goes to that bucket
    when it does not have it: signed now, or by a local publish before.
    """
    signed = project.state / "cache/signed" / signer.key / name / key
    path = _signed_path(project, signer, name, key)
    if not signed.exists() and not remote.fetch_tree(project, path, signed):
        work = signed.with_name(f".{key}")
        shutil.rmtree(work, ignore_errors=True)
        for files in ("RPMS", "SRPMS"):
            shutil.copytree(cached_build(project, name, key) / files, work / files)  # copies: signing changes them
        signer.sign_rpms(project, work)
        work.rename(signed)
    if storage and not remote.tree_exists(storage, path, trusted=dev_signer(project)):
        remote.upload_tree(storage, path, signed, ["RPMS", "SRPMS"], signer=signer)
    return signed


def assemble(
    platforms: list[tuple[Project, dict[str, str]]],
    dest: Path,
    *,
    signer: Signer | None,
    storage: Remote | None,
    build: bool,
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
        _assemble_arch(project, keys, dest, sources[REPO], signer=signer, storage=storage)
        if build:
            _assemble_build(project, dest, sources[BUILD], signer=signer)
    for repo in sources.values():
        repo.index(signer)
    if signer:
        (dest / KEY_FILE).write_text(signer.public_key())


def _assemble_arch(  # noqa: PLR0913
    project: Project,
    keys: dict[str, str],
    dest: Path,
    sources: LocalRepo,
    *,
    signer: Signer | None,
    storage: Remote | None,
) -> None:
    """Make the repository of the binaries of the platform of `project` in `dest`, adding their sources to `sources`."""
    binaries = LocalRepo(project, dest / REPO / project.arch)
    binaries.reset(extra_upstream=False)
    arch, source = f"{REPO}/{project.arch}", f"{REPO}/Source"
    ours = []
    with activity(
        f"signing our packages for {project.arch}" if signer else "getting our packages", log=True
    ) as on_line:
        for name in sorted(keys):
            on_line(name)
            build = (
                _signed_build(project, signer, name, keys[name], storage=storage)
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
    binaries.index(signer)
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
    binaries.index(signer)
    _check_installable(project, [dest / REPO / project.arch, binaries.path])


def _add_upstream(project: Project, names: list[str], dest: Path, binaries: LocalRepo, sources: LocalRepo) -> None:
    """Add the upstream packages `names` to `binaries`, and their sources to `sources`, checking their signatures."""
    with activity(f"getting the upstream packages for {binaries.path.relative_to(dest)}", log=True) as on_line:
        rpms = _upstream_rpms(project, names, on_line)
        for rpm in rpms:
            link(rpm, binaries.path / rpm.name)
    added = [(binaries.path / rpm.name).relative_to(dest).as_posix() for rpm in rpms]
    with activity(f"getting their sources, for {sources.path.relative_to(dest)}", log=True) as on_line:
        for srpm in _upstream_srpms(project, names, on_line):
            if not (sources.path / srpm.name).exists():  # the source of several binaries, or architectures
                link(srpm, sources.path / srpm.name)
                added.append((sources.path / srpm.name).relative_to(dest).as_posix())
    check_signatures(project, dest, added, what="upstream packages")


def _check_installable(project: Project, repos: list[Path]) -> None:
    """Check each package of the last repository of `repos` can be installed from them alone.

    That only needs their metadata: those of a remote snapshot do not have all their packages.
    """
    # named after their checksums
    inputs = [
        sorted(p.name for p in (repo / "repodata").iterdir() if not p.name.startswith("repomd.xml")) for repo in repos
    ]
    key = hashlib.sha256(
        json.dumps([image_tag(project), (BUILDENV / "installable.py").read_text(), inputs]).encode()
    ).hexdigest()
    checked = project.state / "cache/checks/installable" / key
    if checked.exists():
        return
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
    checked.parent.mkdir(parents=True, exist_ok=True)
    checked.touch()


def _tree(project: Project, kind: str, name: str) -> str:
    """Return the path of a snapshot in the storage, of its repositories or its ISOs: `kind` is repos or isos."""
    return f"{kind}/{project.config.publish.release}/{project.config.publish.stage}/{name}"


def snapshot_name(project: Project, storage: Remote) -> str:
    """Return the name of a new snapshot: today's date, with the first suffix used neither by repositories nor ISOs."""
    day = datetime.now(UTC).date().isoformat()
    for name in (day, *(f"{day}.{n}" for n in range(2, 1000))):
        if not (
            remote.exists(storage, f"{_tree(project, 'repos', name)}/{REPO}/Source/repodata/repomd.xml")
            or remote.exists(storage, f"{_tree(project, 'isos', name)}/{remote.MANIFEST}")
        ):
            return name
    msg = f"too many snapshots on {day}"
    raise BuildError(msg)


def _delete_stale(storage: Remote, prefix: str, keep: set[str]) -> None:
    """Delete the files under `prefix` but those of `keep`, and the pages of the directories, written again after."""
    remote.delete(storage, [f"{prefix}/{p}" for p in remote.listing(storage, f"{prefix}/") if p not in keep])


def _batches(files: list[str]) -> list[list[str]]:
    """Return `files` in the batches to write one after the other: the packages, then the metadata listing them,
    then repomd.xml with its signature, as close as can be.
    """  # noqa: D205
    last = [f for f in files if f.endswith(("/repomd.xml", "/repomd.xml.asc"))]
    return [
        [f for f in files if f.endswith(".rpm")],
        [f for f in files if not f.endswith(".rpm") and f not in last],
        last,
    ]


def _upload(  # noqa: PLR0913
    storage: Remote,
    src: Path,
    files: list[str],
    tree: str,
    latest: str,
    *,
    project: Project,
    signer: Signer | None,
    trusted: Signer | None,
    stored: dict[str, tuple[str, str]] | None = None,
    pages: list[str] | None = None,
    remote_files: dict[str, str] | None = None,
) -> None:
    """Upload the `files` of `src` as `tree`, and as `latest` too, and write the pages of their directories and of
    the directories `pages`.

    The files the storage has already, in `latest`, or in `stored`, as path and SHA-256 by file, are copied there,
    like `remote_files`, which are not in `src`, with their SHA-256.
    """  # noqa: D205
    manifest = {f: sha256_of(src / f) for f in files} | (remote_files or {})
    files = sorted(manifest)
    candidates = {
        f: (f"{latest}/{f}", digest) for f, digest in remote.read_manifest(storage, latest, trusted=trusted).items()
    }
    copies = {f: path for f, (path, digest) in (candidates | (stored or {})).items() if manifest.get(f) == digest}
    with activity(f"publishing {tree}", log=True) as on_line:
        for batch in _batches(files):
            remote.transfer(storage, {f"{tree}/{f}": copies.get(f, src / f) for f in batch}, on_line)
        remote.write_manifest(storage, tree, manifest, signer=signer)
        # the files of a failed publish of the same name, and of the previous latest snapshot, but the pages
        keep = {*files, remote.MANIFEST, remote.SIGNATURE}
        _delete_stale(storage, tree, keep)
        for batch in _batches(files):
            remote.transfer(storage, {f"{latest}/{f}": f"{tree}/{f}" for f in batch}, on_line)
        remote.write_manifest(storage, latest, manifest, signer=signer)
        _delete_stale(storage, latest, keep)
        on_line("the pages of the directories")
        remote.update_pages(project, storage, [f"{tree}/", f"{latest}/", *(pages or [])])
    console.print(f"[bold green]✓[/] published {tree}, and as {latest}")


def upload_snapshot(  # noqa: PLR0913
    platforms: list[tuple[Project, dict[str, str]]],
    src: Path,
    storage: Remote,
    name: str,
    *,
    signer: Signer | None,
    stored: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Upload the repositories of the snapshot in `src` of the `platforms` to `storage` as `name`, and make them the
    latest ones.

    The packages already in the storage, in the latest snapshot or the builds with the keys of the platforms, are
    copied there, and so are those of `stored`, which are not in `src`, by path in the snapshot, with their paths in
    the storage and SHA-256.
    """  # noqa: D205
    project = platforms[0][0]
    (src / INPUTS).write_text(SnapshotInputs(keys={
        p.arch: snapshot_key(p, keys, signer=signer, build=(src / BUILD).is_dir()) for p, keys in platforms
    }).model_dump_json())  # fmt: skip
    files = sorted(
        f.relative_to(src).as_posix()
        for f in src.rglob("*")
        if f.is_file() and f.name != OUTPUT_MARKER and not f.is_relative_to(src / ISO_DIR)
    )
    # the trees of the builds: the storage has their packages
    stored = dict(stored or {})
    for platform, keys in platforms:
        for package, key in keys.items():
            tree = _signed_path(platform, signer, package, key) if signer else build_path(platform, package, key)
            for path, digest in remote.read_manifest(storage, tree, trusted=dev_signer(project)).items():
                if repo := {"RPMS": f"{REPO}/{platform.arch}", "SRPMS": f"{REPO}/Source"}.get(path.split("/")[0]):
                    stored[f"{repo}/{Path(path).name}"] = (f"{tree}/{path}", digest)
    signed = [f"signed/{signer.key}/{platform.arch}/" for platform, _ in platforms] if signer else []
    repos, latest = _tree(project, "repos", name), _tree(project, "repos", "latest")
    _upload(
        storage,
        src,
        files,
        repos,
        latest,
        project=project,
        signer=signer or signing_key(project),
        trusted=dev_signer(project),
        stored=stored,
        pages=signed,
        remote_files={f: digest for f, (_, digest) in stored.items() if f not in files},
    )


def upload_isos(  # noqa: PLR0913
    project: Project,
    src: Path,
    storage: Remote,
    name: str,
    *,
    signer: Signer | None,
    stored: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Upload the ISOs of the snapshot in `src`, in ISO_DIR, to `storage` as `name`, and make them the latest ones."""
    from xcraft.iso import iso_inputs  # noqa: PLC0415, iso imports snapshot assembly

    isos = src / ISO_DIR
    (isos / INPUTS).write_text(SnapshotInputs(keys={
        name: key for p in project.all_platforms() if (isos / p.arch).is_dir()
        for name, key in iso_inputs(p, signer=signer).items()
    }).model_dump_json())  # fmt: skip
    files = sorted(f.relative_to(isos).as_posix() for f in isos.rglob("*") if f.is_file())
    _upload(
        storage,
        isos,
        files,
        _tree(project, "isos", name),
        _tree(project, "isos", "latest"),
        project=project,
        signer=signer or signing_key(project),
        trusted=dev_signer(project),
        stored=stored,
    )


def _built(project: Project, *, fetch: Fetch) -> dict[str, str]:
    """Build or fetch our packages, but only fetch them on the platforms the host emulates, and return their keys."""
    prepare(project)
    results, keys = builder.build(project, sorted(project.packages), build_missing=project.native, fetch=fetch)
    if failed := [r.name for r in results if r.failed]:
        msg = f"not built for {project.arch}: {', '.join(failed)}"
        raise RuntimeError(msg)
    return keys


def _summary(dest: Path, built: list[tuple[Project, dict[str, str]]], *, build: bool) -> None:
    console.print(
        f"[bold green]✓[/] snapshot in {dest}: "
        + ", ".join(
            f"{p.arch}: {len(keys)} packages, {len(load_lock(p).runtime.get(p.arch, []))} upstream ones"
            + (f", and {len(build_dependencies(p))} to build them" if build else "")
            for p, keys in built
        )
    )


def snapshot(
    platforms: list[Project], dest: Path, *, signer: Signer | None, storage: Remote | None, build: bool
) -> list[tuple[Project, dict[str, str]]]:
    """Make the snapshot of the `platforms` in `dest`, and return them with the keys of their packages.

    Our packages are built or fetched first, but only fetched for the platforms the host emulates. With `build`, the
    snapshot has the build repository too.
    """
    built = []
    for project in platforms:
        keys = _built(project, fetch="all")
        resolve_runtime(project, builds_repo(project, keys))
        built.append((project, keys))
    assemble(built, dest, signer=signer, storage=storage, build=build)
    _summary(dest, built, build=build)
    return built


@dataclass
class _Plan:
    """A snapshot made with the packages the storage misses only, and the metadata of all of them."""

    project: Project
    dest: Path
    known: dict[str, tuple[Entry, str]]
    """The metadata of the packages of the latest snapshot, by SHA-256, with their paths in the storage."""
    sources: dict[str, str]
    """The SHA-256 of the source packages of the latest snapshot, by file name."""
    stored: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    """The packages the storage has, by repository and file name, with their paths there and SHA-256."""

    def has(self, repo: str, name: str) -> bool:
        """Return whether the repository `repo` of the snapshot has the package `name`."""
        return name in self.stored.get(repo, {}) or (self.dest / repo / name).exists()

    def reuse(self, repo: str, name: str, digest: str, path: str | None = None) -> bool:
        """Add the package `name` with `digest` to `repo`, and return whether the storage has it, as `path` or in the
        latest snapshot, with its metadata: otherwise, it must be put in the directory of the repository.
        """  # noqa: D205
        if digest not in self.known:
            return False
        self.stored.setdefault(repo, {})[name] = (path or self.known[digest][1], digest)
        return True

    def index(self, repo: str, signer: Signer | None) -> Path:
        """Write the metadata of `repo`, signed by `signer`, if any, and return its directory."""
        path = self.dest / repo
        path.mkdir(parents=True, exist_ok=True)
        entries = {name: self.known[digest][0] for name, (_, digest) in self.stored.get(repo, {}).items()}
        if rpms := sorted(path.glob("*.rpm")):
            run(
                self.project,
                "repo",
                "createrepo_c -q --general-compress-type=gz /repo",
                volumes=[f"{path}:/repo"],
                show=f"indexing the new packages of {repo}",
                network=False,
            )
            local = repodata.read(path) or {}
            entries |= {rpm.name: local[sha256_of(rpm)] for rpm in rpms}
        repodata.write(path, entries)
        if signer:
            signer.sign_file(path / "repodata/repomd.xml")
        return path

    def files(self) -> dict[str, tuple[str, str]]:
        """Return the packages the storage has, by path in the snapshot, with their paths there and SHA-256."""
        return {f"{repo}/{name}": stored for repo, files in self.stored.items() for name, stored in files.items()}


def _previous(project: Project, storage: Remote) -> tuple[dict[str, tuple[Entry, str]], dict[str, str]]:
    """Return the metadata of the packages of the latest snapshot, by SHA-256, with their paths in the storage, and
    the SHA-256 of its source packages, by file name.

    Only the metadata matching its trusted manifest is read, for the packages with the SHA-256 of the manifest: none
    of a repository whose metadata Python cannot decompress, like zstd before Python 3.14.
    """  # noqa: D205
    latest = _tree(project, "repos", "latest")
    manifest = remote.read_manifest(storage, latest, trusted=dev_signer(project))
    known = {}
    with project.work("previous") as work, activity("reading the metadata of the latest snapshot", log=True):

        def fetch(path: str) -> bool:
            return remote.download(storage, f"{latest}/{path}", work / path) and sha256_of(work / path) == manifest.get(
                path
            )

        for repomd in (f for f in manifest if f.endswith("/repodata/repomd.xml")):
            repo = repomd.removesuffix("/repodata/repomd.xml")
            if not fetch(repomd):
                continue
            hrefs = repodata.files((work / repomd).read_bytes()).values()
            with ThreadPoolExecutor(remote.REQUESTS) as pool:
                if not all(pool.map(fetch, [f"{repo}/{href}" for href in hrefs])):
                    continue
            for digest, entry in (repodata.read(work / repo) or {}).items():
                if manifest.get(path := f"{repo}/{repodata.location(entry)}") == digest:
                    known[digest] = (entry, f"{latest}/{path}")
    sources = {
        Path(f).name: digest for f, digest in manifest.items() if f.startswith((f"{REPO}/Source/", f"{BUILD}/Source/"))
    }
    return known, sources


def _stored_build(project: Project, signer: Signer | None, name: str, key: str, storage: Remote) -> Path | str:
    """Return the tree of the build of `name` with `key`, signed by `signer`, if any, in `storage`, or the local
    one when the storage misses it: signed now, and pushed there.
    """  # noqa: D205
    tree = _signed_path(project, signer, name, key) if signer else build_path(project, name, key)
    if remote.tree_exists(storage, tree, trusted=dev_signer(project)):
        return tree
    if not cached_build(project, name, key).exists() and not builder.fetch_build(project, name, key):
        msg = f"the build of {name} is neither cached nor in the remote storage"
        raise BuildError(msg)
    return _signed_build(project, signer, name, key, storage=storage) if signer else cached_build(project, name, key)


def _put(storage: Remote, tree: Path | str, path: str, dest: Path, digest: str) -> None:
    """Put the file `path` of the local or remote `tree` as `dest`, checking its SHA-256."""
    if isinstance(tree, Path):
        link(tree / path, dest)
    elif not remote.download(storage, f"{tree}/{path}", dest) or sha256_of(dest) != digest:
        msg = f"{tree}/{path} is missing or corrupted in the storage"
        raise BuildError(msg)


def _add_ours(plan: _Plan, project: Project, keys: dict[str, str], *, signer: Signer | None, storage: Remote) -> None:
    """Add our packages to the base repository of the platform of `project`, and their sources."""
    arch, source = f"{REPO}/{project.arch}", f"{REPO}/Source"
    for repo in (arch, source):
        (plan.dest / repo).mkdir(parents=True, exist_ok=True)
    local = []
    with activity(
        f"signing our packages for {project.arch}" if signer else "getting our packages", log=True
    ) as on_line:
        for name in sorted(keys):
            on_line(name)
            tree = _stored_build(project, signer, name, keys[name], storage)
            if isinstance(tree, Path):
                files = {p.relative_to(tree).as_posix(): sha256_of(p) for p in tree.rglob("*.rpm")}
            else:
                files = remote.read_manifest(storage, tree, trusted=dev_signer(project))
            for path, digest in sorted(files.items()):
                repo, file = {"RPMS": arch, "SRPMS": source}.get(path.split("/")[0]), Path(path).name
                if not repo or (repo == source and plan.has(repo, file)):
                    continue
                if not plan.reuse(repo, file, digest, None if isinstance(tree, Path) else f"{tree}/{path}"):
                    _put(storage, tree, path, plan.dest / repo / file, digest)
                    local.append(f"{repo}/{file}")
    # those of the storage are in trees it trusts: checked when signed
    if signer:
        check_signatures(project, plan.dest, local, what="packages of ours", public_key=signer.public_key())
    for rpm in extra_rpms(project):  # neither ours nor from the repositories: as they are
        if not plan.reuse(arch, rpm.name, sha256_of(rpm)):
            link(rpm, plan.dest / arch / rpm.name)


def _add_stored_upstream(plan: _Plan, project: Project, names: list[str], repo: str, source: str) -> None:
    """Add the upstream packages `names` to `repo`, and their sources to `source`, checking the signatures of those
    the storage misses: those it has were when added.
    """  # noqa: D205
    locked = resolve_image(project).packages
    rpms = {name: f"{name}-{locked[name].evr.split(':')[-1]}.{locked[name].arch}.rpm" for name in names}
    missing = [name for name in names if not plan.reuse(repo, rpms[name], locked[name].sha256)]
    with activity(f"getting the upstream packages for {repo}", log=True) as on_line:
        (plan.dest / repo).mkdir(parents=True, exist_ok=True)
        for rpm in _upstream_rpms(project, missing, on_line):
            link(rpm, plan.dest / repo / rpm.name)
    added = [f"{repo}/{rpms[name]}" for name in missing]
    srpms = {name: repodata.sourcerpm(plan.known[locked[name].sha256][0]) for name in names if name not in missing}
    with activity(f"getting their sources, for {source}", log=True) as on_line, _sources(project) as (work, volumes):
        srpms |= _srpm_names(project, [name for name in names if not srpms.get(name)], work, volumes)
        new = {
            srpm: _nevra(locked, name)
            for name, srpm in srpms.items()
            if not plan.has(source, srpm) and not plan.reuse(source, srpm, plan.sources.get(srpm, ""))
        }
        cache = _download(project, new, on_line, source=True, volumes=volumes)
        (plan.dest / source).mkdir(parents=True, exist_ok=True)
        for srpm in new:
            link(cache / srpm, plan.dest / source / srpm)
            added.append(f"{source}/{srpm}")
    check_signatures(project, plan.dest, added, what="upstream packages")


def remote_snapshot(
    platforms: list[Project], dest: Path, *, signer: Signer | None, storage: Remote, build: bool
) -> tuple[list[tuple[Project, dict[str, str]]], dict[str, tuple[str, str]]]:
    """Make the snapshot of the `platforms` in `dest` like snapshot(), but only with the packages `storage` misses,
    and return the platforms with the keys of their packages, and the packages it has, by path in the snapshot,
    with their paths there and SHA-256.

    The metadata of the packages of the latest snapshot are those of its own metadata, and our packages are only
    fetched when the storage misses their builds: neither is downloaded. The repositories are checked with their
    metadata only.
    """  # noqa: D205
    project = platforms[0]
    fresh_dir(project, dest)
    plan = _Plan(project, dest, *_previous(project, storage))
    built = []
    for platform in platforms:
        keys = _built(platform, fetch="needed")
        arch = f"{REPO}/{platform.arch}"
        _add_ours(plan, platform, keys, signer=signer, storage=storage)
        resolve_runtime(platform, LocalRepo(platform, plan.index(arch, None), up_to_date=True))
        _add_stored_upstream(plan, platform, load_lock(platform).runtime.get(platform.arch, []), arch, f"{REPO}/Source")
        _check_installable(platform, [plan.index(arch, signer)])
        if build:
            _add_stored_upstream(
                plan, platform, build_dependencies(platform), f"{BUILD}/{platform.arch}", f"{BUILD}/Source"
            )
            _check_installable(platform, [plan.dest / arch, plan.index(f"{BUILD}/{platform.arch}", signer)])
        built.append((platform, keys))
    for repo in (REPO, BUILD) if build else (REPO,):
        plan.index(f"{repo}/Source", signer)
    if signer:
        (dest / KEY_FILE).write_text(signer.public_key())
    _summary(dest, built, build=build)
    return built, plan.files()


def materialize(storage: Remote, dest: Path, files: dict[str, tuple[str, str]], repo: str) -> None:
    """Download to `dest` the packages of `repo` the storage has, `files`, as remote_snapshot() returns them."""
    wanted = {f: stored for f, stored in files.items() if f.startswith(f"{repo}/") and not (dest / f).exists()}

    def fetch(f: str) -> None:
        path, digest = wanted[f]
        if not remote.download(storage, path, dest / f) or sha256_of(dest / f) != digest:
            msg = f"{path} is missing or corrupted in the storage"
            raise BuildError(msg)

    with activity(f"downloading the packages of {repo}", log=True), ThreadPoolExecutor(remote.REQUESTS) as pool:
        list(pool.map(fetch, wanted))
