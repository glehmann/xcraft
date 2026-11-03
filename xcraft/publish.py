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
from xcraft import remote, repodata, upstream_copy
from xcraft.cache import BuildError, build_keys, build_path, cached_build
from xcraft.container import BUILDENV, buildenv_hash, run, run_natively
from xcraft.display import activity, console
from xcraft.localrepo import OUTPUT_MARKER, extra_rpms, fresh_dir, link
from xcraft.lock import load_lock
from xcraft.pinned import pins
from xcraft.project import INTERNAL
from xcraft.resolution import prepare, published, resolve
from xcraft.signing import Signer, check_signatures, dev_signer, signing_key
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from xcraft.build import Fetch
    from xcraft.lock import Pin
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
SUMS = "SHA256SUMS"
"""The checksums of the published ISOs, as sha256sum writes them, next to them."""
INPUTS = "inputs.json"
"""What the latest snapshot of a stage was made from, a SnapshotInputs, in the tree inputs_tree() of the storage."""


class SnapshotInputs(BaseModel):
    """The input keys of the architectures or ISO variants of the latest snapshot, and its files, with their SHA-256:
    it is reused while they are the same.
    """

    keys: dict[str, str]
    files: dict[str, str]


def inputs_tree(latest: str) -> str:
    """Return the tree of what the latest snapshot `latest` was made from, out of it, in the storage."""
    return f"{INTERNAL}/inputs/{latest}"


def snapshot_key(project: Project, keys: dict[str, str], *, signer: Signer | None, build: bool) -> str:
    """Return the key of the snapshot of the platform of `project`, with the cache `keys` of our packages: what it is
    made of, read without its packages, nor containers.
    """
    lock = load_lock(project)
    data = [
        project.platform, project.config.platforms, keys, lock.sources, lock.runtime.get(project.arch, []),
        project.config.publish.extra, project.config.iso.installer,
        {n: e.model_dump() for n, e in project.config.extra_upstream.items()},
        {n: repo.model_dump() for n, repo in project.repos.items()},
        {n: p.text() for n, p in lock.binaries.get(project.arch, {}).items()},
        signer.key if signer else None, build, buildenv_hash(),
        # how snapshots are made: whole modules, rather than a list of what they use, easy to get incomplete
        [Path(__file__).with_name(module).read_text() for module in ("publish.py", "localrepo.py", "repodata.py")],
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _trusted(project: Project, signer: Signer | None) -> Signer | None:
    """Return the key the trees of the storage are trusted signed with, besides those of xcraft.toml: the one signing
    the snapshot, or the developer's.
    """
    return signer or dev_signer(project)


def _reusable(
    project: Project, storage: Remote, kind: str, keys: dict[str, str], signer: Signer | None
) -> dict[str, str]:
    latest, trusted = _tree(project, kind, "latest"), _trusted(project, signer)
    with project.work("snapshot-inputs") as work:
        try:
            if not remote.download_tree(storage, inputs_tree(latest), work / "inputs", trusted=trusted):
                return {}
            inputs = SnapshotInputs.model_validate_json((work / "inputs" / INPUTS).read_bytes())
        except (OSError, ValueError, remote.RemoteError):
            return {}
    manifest = remote.read_manifest(storage, latest, trusted=trusted)
    return manifest if manifest and inputs.keys == keys and inputs.files == manifest else {}


def reuse_published(
    platforms: list[Project],
    storage: Remote,
    *,
    signer: Signer | None,
    build: bool,
    only: str | None,
    isos: list[Project],
) -> bool:
    """Publish the latest snapshot again, as a new one, by copies in the storage, when it is trusted and made of the
    same, and return whether it was.
    """
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
    for i, (kind, manifest) in enumerate(manifests.items()):
        latest, target = _tree(project, kind, "latest"), _tree(project, kind, name)
        with activity(f"reusing the published {kind} as {target}", log=True) as on_line:
            try:
                for files in _batches(list(manifest)):
                    remote.transfer(storage, {f"{target}/{f}": f"{latest}/{f}" for f in files}, on_line)
            except remote.RemoteError as e:  # a file missing in the latest snapshot: all assembled again, as `name`
                console.print(f"[yellow]the published {kind} cannot be reused[/]: {escape(str(e))}")
                for copied in list(manifests)[: i + 1]:
                    _delete_stale(storage, _tree(project, copied, name), set())
                return False
            remote.write_manifest(storage, target, manifest, signer=signer or signing_key(project))
            _delete_stale(storage, target, {*manifest, remote.MANIFEST})
            remote.update_pages(project, storage, [f"{target}/", f"{latest}/"])
        console.print(f"[bold green]✓[/] published {target}, and as {latest} (reused the matching snapshot)")
    return True


def _download(
    project: Project,
    files: dict[str, str],
    on_line: Callable[[str], None],
    *,
    source: bool,
    available: bool = False,
) -> Path:
    """Download the upstream packages `files`, as file name: NEVRA, to their cache, and return it: those found only,
    if `available`, without failing for the others.

    Those not in the cache come from the latest snapshot, the upstream copy, see xcraft.upstream_copy, or from
    the repositories, which may not have them anymore, after an update, while the storage may not be there: natively,
    for any architecture. The signatures of the source packages are checked before the cache, which would keep a bad
    one.
    """
    cache = project.state / ("cache/srpms" if source else "cache/rpms")
    if not (missing := {f: nevra for f, nevra in files.items() if not (cache / f).exists()}):
        return cache
    with project.work("download") as work:
        if storage := project.config.remote.get("main"):
            latest, directory = _tree(project, "repos", "latest"), "Source" if source else project.arch
            copy = f"{upstream_copy.PREFIX}/{directory}"
            paths = [f"{latest}/{REPO}/{directory}", f"{latest}/{BUILD}/{directory}", copy]

            def fetch(f: str) -> None:
                for path in paths:  # where it is, if anywhere
                    if remote.download(storage, f"{path}/{f}", work / f):
                        on_line(f)
                        return

            with contextlib.suppress(remote.RemoteError), ThreadPoolExecutor(remote.REQUESTS) as pool:
                list(pool.map(fetch, missing))
        if left := sorted({nevra for f, nevra in missing.items() if not (work / f).exists()}):
            options = [*(["--source"] if source else []), *(["--setopt=strict=0"] if available else [])]
            run_natively(
                project,
                "download",
                ["dnf", "-q", "download", f"--forcearch={project.arch}", *options, "--destdir", "/work", *left],
                volumes=[f"{work}:/work"],
                show=f"downloading {len(left)} upstream {'source ' if source else ''}packages of {project.arch}",
                repos=_source_repos(project) if source else None,
            )
        found = [f for f in missing if (work / f).exists()]
        if source and found:
            check_signatures(project, work, found, what="upstream source packages")
        cache.mkdir(parents=True, exist_ok=True)
        for f in found:
            (work / f).rename(cache / f)
    return cache


def _nevra(packages: dict[str, Pin], name: str) -> str:
    return f"{name}-{packages[name].evr}.{packages[name].arch}"


def _rpm_name(packages: dict[str, Pin], name: str) -> str:
    return f"{name}-{packages[name].evr.split(':')[-1]}.{packages[name].arch}.rpm"


def _upstream_rpms(project: Project, names: list[str], on_line: Callable[[str], None]) -> list[Path]:
    """Return the RPMs of pinned upstream packages: from the cache, the latest snapshot, or the repositories."""
    locked = pins(project)
    rpms = {name: _rpm_name(locked, name) for name in names}
    cache = _download(project, {rpms[name]: _nevra(locked, name) for name in names}, on_line, source=False)
    for name in names:
        if sha256_of(cache / rpms[name]) != locked[name].sha256:
            (cache / rpms[name]).unlink()
            msg = f"{rpms[name]} does not have the SHA-256 of its pin in the lock"
            raise BuildError(msg)
    return [cache / rpm for rpm in rpms.values()]


def download_sources(project: Project, files: dict[str, str]) -> Path:
    """Download those of the upstream source packages `files`, as file name: NEVRA of one of their binary packages,
    that are found, to their cache, and return it, like _download().
    """
    return _download(project, files, lambda _: None, source=True, available=True)


def _source_repos(project: Project) -> dict[str, str]:
    """Return the repositories of the upstream source packages, by file name, only for their downloads, not to change
    the build environment.
    """
    return {
        "xcraft-sources.repo": "".join(
            f"[{name}-source]\nname={name} sources\nbaseurl={repo.source_baseurl}\nenabled=0\ngpgcheck=0\n"
            for name, repo in project.repos.items()
            if repo.source_baseurl
        )
    }


def _srpm_names(project: Project, names: list[str]) -> dict[str, str]:
    """Return the source packages of the locked upstream packages `names`, as in the metadata of the repositories,
    natively, for any architecture.
    """
    if not names:  # dnf repoquery would list everything
        return {}
    locked = pins(project)
    nevras = {name: _nevra(locked, name) for name in names}
    with project.work("srpms") as work:
        run_natively(
            project,
            "srpms",
            [
                "bash",
                "-c",
                (
                    f"dnf -q repoquery --forcearch={project.arch} --qf '%{{name}} %{{sourcerpm}}' "
                    f"{' '.join(nevras.values())} > /work/srpms"
                ),
            ],
            volumes=[f"{work}:/work"],
            show=f"looking for the source packages of {len(names)} upstream packages of {project.arch}",
        )
        lines = (work / "srpms").read_text().splitlines()
    srpms = {n: srpm for n, srpm in (line.split() for line in lines if line) if n in nevras}
    if unknown := sorted(set(names) - srpms.keys()):
        msg = f"no source package for: {', '.join(unknown)}"
        raise BuildError(msg)
    return srpms


def _signed_path(project: Project, signer: Signer, name: str, key: str) -> str:
    """Return the path of the tree of a signed build in the remote storage."""
    return f"{INTERNAL}/signed/{signer.key}/{project.arch}/{name}/{key}"


def _stored_path(project: Project, signer: Signer | None, name: str, key: str) -> str:
    """Return the path of the tree of a build in the remote storage: signed by `signer`, if any."""
    return _signed_path(project, signer, name, key) if signer else build_path(project, name, key)


def _signed_build(project: Project, signer: Signer, name: str, key: str, *, storage: Remote | None) -> Path:
    """Return the build of `name` with `key`, signed by `signer`: from the cache, the remote storage, or signed now.

    A build is signed once: the signatures have a date, so signing again would not give the same files, which the
    storage could not store once for all the snapshots. With `storage`, the signed build goes to that bucket
    when it does not have it: signed now, or by a local publish before.
    """
    # by architecture, as the builds of all of them have the same keys
    signed = project.state / "cache/signed" / signer.key / project.arch / name / key
    path = _signed_path(project, signer, name, key)
    if not signed.exists() and not remote.fetch_tree(project, path, signed):
        work = signed.with_name(f".{key}")
        shutil.rmtree(work, ignore_errors=True)
        for files in ("RPMS", "SRPMS"):
            shutil.copytree(cached_build(project, name, key) / files, work / files)  # copies: signing changes them
        signer.sign_rpms(project, work)
        work.rename(signed)
    if storage and not remote.tree_exists(storage, path, trusted=signer):
        remote.upload_tree(storage, path, signed, ["RPMS", "SRPMS"], signer=signer)
    return signed


def build_dependencies(project: Project) -> list[str]:
    """Return the upstream packages the builds of ours need, but those of the base repository.

    Those are the packages of the image of the build environment and of the build roots, but the upstream ones ours
    replace, like kernel-headers.
    """
    resolved, runtime = resolve(project), load_lock(project).runtime.get(project.arch, [])
    ours = {binary.name for package in project.packages.values() for binary in package.binaries}
    return sorted({*resolved.image, *(n for names in resolved.buildroots.values() for n in names)} - ours - {*runtime})


def _check_installable(project: Project, repos: list[Path]) -> None:
    """Check each package of the last repository of `repos` can be installed from them alone.

    That only needs their metadata: those of a remote snapshot do not have all their packages.
    """
    # named after their checksums
    inputs = [
        sorted(p.name for p in (repo / "repodata").iterdir() if not p.name.startswith("repomd.xml")) for repo in repos
    ]
    key = hashlib.sha256(
        json.dumps([project.config.base_image, (BUILDENV / "installable.py").read_text(), inputs]).encode()
    ).hexdigest()
    checked = project.state / "cache/checks/installable" / key
    if checked.exists():
        return
    with project.work("snapshot") as work:
        run_natively(
            project,
            "snapshot",
            ["python3", "/xcraft/installable.py", project.arch, "/work/errors.json"]
            + [f"/repos/{i}" for i in range(len(repos))],
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
    """
    last = [f for f in files if f.endswith(("/repomd.xml", "/repomd.xml.asc"))]
    return [
        [f for f in files if f.endswith(".rpm")],
        [f for f in files if not f.endswith(".rpm") and f not in last],
        last,
    ]


def _upload(
    storage: Remote,
    src: Path,
    files: list[str],
    tree: str,
    latest: str,
    *,
    project: Project,
    inputs: dict[str, str],
    signer: Signer | None,
    trusted: Signer | None,
    stored: dict[str, tuple[str, str]] | None = None,
    pages: list[str] | None = None,
    remote_files: dict[str, str] | None = None,
) -> None:
    """Upload the `files` of `src` as `tree`, and as `latest` too, made from the input keys `inputs`, and write the
    pages of their directories and of the directories `pages`.

    The files the storage has already, in `latest`, or in `stored`, as path and SHA-256 by file, are copied there,
    like `remote_files`, which are not in `src`, with their SHA-256.
    """
    manifest = {f: sha256_of(src / f) for f in files} | (remote_files or {})
    files = sorted(manifest)
    previous = remote.read_manifest(storage, latest, trusted=trusted)
    candidates = {f: (f"{latest}/{f}", digest) for f, digest in previous.items()}
    copies = {f: path for f, (path, digest) in (candidates | (stored or {})).items() if manifest.get(f) == digest}
    with activity(f"publishing {tree}", log=True) as on_line:
        for batch in _batches(files):
            remote.transfer(storage, {f"{tree}/{f}": copies.get(f, src / f) for f in batch}, on_line)
        remote.write_manifest(storage, tree, manifest, signer=signer)
        # the files of a failed publish of the same name, and of the previous latest snapshot, but the pages
        keep = {*files, remote.MANIFEST}
        _delete_stale(storage, tree, keep)
        # only the files changed: without its manifest, a latest tree left incomplete is copied entirely next time
        remote.delete(storage, [f"{latest}/{remote.MANIFEST}"])
        for batch in _batches([f for f in files if previous.get(f) != manifest[f]]):
            remote.transfer(storage, {f"{latest}/{f}": f"{tree}/{f}" for f in batch}, on_line)
        remote.write_manifest(storage, latest, manifest, signer=signer)
        _delete_stale(storage, latest, keep)
        with project.work("snapshot-inputs") as work:
            (work / INPUTS).write_text(SnapshotInputs(keys=inputs, files=manifest).model_dump_json())
            remote.upload_tree(storage, inputs_tree(latest), work, [INPUTS], signer=signer)
        on_line("the pages of the directories")
        remote.update_pages(project, storage, [f"{tree}/", f"{latest}/", f"{inputs_tree(latest)}/", *(pages or [])])
    console.print(f"[bold green]✓[/] published {tree}, and as {latest}")


def upload_snapshot(
    platforms: list[tuple[Project, dict[str, str]]],
    src: Path,
    storage: Remote,
    name: str,
    *,
    signer: Signer | None,
    build: bool,
    stored: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Upload the repositories of the snapshot in `src` of the `platforms` to `storage` as `name`, and make them the
    latest ones: with the build repository, with `build`.

    The packages already in the storage, in the latest snapshot or the builds with the keys of the platforms, are
    copied there, and so are those of `stored`, which are not in `src`, by path in the snapshot, with their paths in
    the storage and SHA-256.
    """
    project = platforms[0][0]
    files = sorted(
        f.relative_to(src).as_posix()
        for f in src.rglob("*")
        if f.is_file() and f.name != OUTPUT_MARKER and not f.is_relative_to(src / ISO_DIR)
    )
    # the trees of the builds: the storage has their packages
    stored = dict(stored or {})
    for platform, keys in platforms:
        for package, key in keys.items():
            tree = _stored_path(platform, signer, package, key)
            for path, digest in remote.read_manifest(storage, tree, trusted=_trusted(project, signer)).items():
                if repo := {"RPMS": f"{REPO}/{platform.arch}", "SRPMS": f"{REPO}/Source"}.get(path.split("/")[0]):
                    # those of the metadata, of the snapshot or of the first architecture, for the source packages
                    stored.setdefault(f"{repo}/{Path(path).name}", (f"{tree}/{path}", digest))
    signed = [f"{INTERNAL}/signed/{signer.key}/{platform.arch}/" for platform, _ in platforms] if signer else []
    repos, latest = _tree(project, "repos", name), _tree(project, "repos", "latest")
    _upload(
        storage,
        src,
        files,
        repos,
        latest,
        project=project,
        inputs={p.arch: snapshot_key(p, keys, signer=signer, build=build) for p, keys in platforms},
        signer=signer or signing_key(project),
        trusted=_trusted(project, signer),
        stored=stored,
        pages=signed,
        remote_files={f: digest for f, (_, digest) in stored.items() if f not in files},
    )


def upload_isos(
    project: Project,
    src: Path,
    storage: Remote,
    name: str,
    *,
    signer: Signer | None,
    stored: dict[str, tuple[str, str]] | None = None,
) -> None:
    """Upload the ISOs of the snapshot in `src`, in ISO_DIR, of all the architectures, to `storage` as `name`, with
    their checksums, in SHA256SUMS, signed by `signer`, if any, and make them the latest ones.
    """
    from xcraft.iso import iso_inputs  # noqa: PLC0415, iso imports snapshot assembly

    isos = src / ISO_DIR
    sums = isos / SUMS
    sums.write_text("".join(f"{sha256_of(iso)}  {iso.name}\n" for iso in sorted(isos.glob("*.iso"))))
    if signer:
        signer.sign_file(sums)
    files = sorted(f.relative_to(isos).as_posix() for f in isos.rglob("*") if f.is_file())
    _upload(
        storage,
        isos,
        files,
        _tree(project, "isos", name),
        _tree(project, "isos", "latest"),
        project=project,
        inputs={
            name: key
            for p in project.all_platforms()
            if any(iso.name.endswith((f"-{p.arch}.iso", f"-{p.arch}-netinstall.iso")) for iso in isos.glob("*.iso"))
            for name, key in iso_inputs(p, signer=signer).items()
        },
        signer=signer or signing_key(project),
        trusted=_trusted(project, signer),
        stored=stored,
    )


def _built(project: Project, *, fetch: Fetch) -> dict[str, str]:
    """Build or fetch our packages, but only fetch them on the platforms the host emulates, and return their keys."""
    prepare(project)
    results, keys, _ = builder.build(project, sorted(project.packages), build_missing=project.native, fetch=fetch)
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


@dataclass
class _Plan:
    """A snapshot: the packages the storage misses, all without one, in its directory, and the metadata of all of
    them.
    """

    project: Project
    dest: Path
    known: dict[str, tuple[Entry, str]]
    """The metadata of the packages of the latest snapshot and of the upstream copy, by SHA-256, with their paths in
    the storage."""
    sources: dict[str, str]
    """The SHA-256 of the source packages of the latest snapshot and of the upstream copy, by file name."""
    stored: dict[str, dict[str, tuple[str, str]]] = field(default_factory=dict)
    """The packages the storage has, by repository and file name, with their paths there and SHA-256."""

    def has(self, repo: str, name: str) -> bool:
        """Return whether the repository `repo` of the snapshot has the package `name`."""
        return name in self.stored.get(repo, {}) or (self.dest / repo / name).exists()

    def reuse(self, repo: str, name: str, digest: str, path: str | None = None) -> bool:
        """Add the package `name` with `digest` to `repo`, and return whether the storage has it, as `path` or in the
        latest snapshot, with its metadata: otherwise, it must be put in the directory of the repository.
        """
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
                self.project.on_host(),  # natively: the metadata does not depend on the architecture
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


def previous(
    project: Project, storage: Remote, signer: Signer | None
) -> tuple[dict[str, tuple[Entry, str]], dict[str, str]]:
    """Return the metadata of the packages of the latest snapshot, by SHA-256, with their paths in the storage, and
    the SHA-256 of its source packages, by file name.

    Only the metadata matching its trusted manifest is read, for the packages with the SHA-256 of the manifest: none
    of a repository whose metadata Python cannot decompress, like zstd before Python 3.14.
    """
    latest = _tree(project, "repos", "latest")
    manifest = remote.read_manifest(storage, latest, trusted=_trusted(project, signer))
    known = {}
    with project.work("previous") as work, activity("reading the metadata of the latest snapshot", log=True):

        def fetch(path: str) -> bool:
            # of the manifest only, whose paths stay in the tree
            return (
                path in manifest
                and remote.download(storage, f"{latest}/{path}", work / path)
                and (sha256_of(work / path) == manifest[path])
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


def _with_copy(
    platforms: list[Project], storage: Remote, signer: Signer | None
) -> tuple[dict[str, tuple[Entry, str]], dict[str, str]]:
    """Return the packages of the latest snapshot, like previous(), and those of the upstream copy of the storage, for
    the architectures of `platforms`: copied from there, without being uploaded.
    """
    known, sources = upstream_copy.stored(platforms[0], storage, [p.arch for p in platforms])
    latest, latest_sources = previous(platforms[0], storage, signer)
    return known | latest, sources | latest_sources


def _stored_build(
    project: Project, signer: Signer | None, name: str, key: str, storage: Remote | None
) -> tuple[Path | str, dict[str, str]]:
    """Return the tree of the build of `name` with `key`, signed by `signer`, if any, in `storage`, or the local
    one when there is no storage, or it misses it: signed now, and pushed there; with its files, by path in it, and
    their SHA-256.
    """
    tree = _stored_path(project, signer, name, key)
    if storage and (files := remote.read_manifest(storage, tree, trusted=_trusted(project, signer))):
        return tree, files
    if not cached_build(project, name, key).exists() and not builder.fetch_build(project, name, key):
        msg = f"the build of {name} is neither cached nor in the remote storage"
        raise BuildError(msg)
    local = _signed_build(project, signer, name, key, storage=storage) if signer else cached_build(project, name, key)
    return local, {p.relative_to(local).as_posix(): sha256_of(p) for p in local.rglob("*.rpm")}


def _put(storage: Remote | None, tree: Path | str, path: str, dest: Path, digest: str) -> None:
    """Put the file `path` of the local `tree`, or of the one of `storage`, as `dest`, checking its SHA-256."""
    if isinstance(tree, Path):
        link(tree / path, dest)
    elif not (storage and remote.download(storage, f"{tree}/{path}", dest)) or sha256_of(dest) != digest:
        msg = f"{tree}/{path} is missing or corrupted in the storage"
        raise BuildError(msg)


def _add_ours(
    plan: _Plan, project: Project, keys: dict[str, str], *, signer: Signer | None, storage: Remote | None
) -> None:
    """Add our packages to the base repository of the platform of `project`, and their sources."""
    arch, source = f"{REPO}/{project.arch}", f"{REPO}/Source"
    for repo in (arch, source):
        (plan.dest / repo).mkdir(parents=True, exist_ok=True)
    local = []
    with activity(
        f"signing our packages for {project.arch}" if signer else "getting our packages", log=True
    ) as on_line:
        if storage:  # their manifests, checked once, concurrently
            trusted, trees = _trusted(project, signer), [_stored_path(project, signer, n, k) for n, k in keys.items()]
            with ThreadPoolExecutor(remote.REQUESTS) as pool:
                list(pool.map(lambda tree: remote.read_manifest(storage, tree, trusted=trusted), trees))
        for name in sorted(keys):
            on_line(name)
            tree, files = _stored_build(project, signer, name, keys[name], storage)
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
    """
    locked = pins(project)
    rpms = {name: _rpm_name(locked, name) for name in names}
    missing = [name for name in names if not plan.reuse(repo, rpms[name], locked[name].sha256)]
    with activity(f"getting the upstream packages for {repo}", log=True) as on_line:
        (plan.dest / repo).mkdir(parents=True, exist_ok=True)
        for rpm in _upstream_rpms(project, missing, on_line):
            link(rpm, plan.dest / repo / rpm.name)
    added = [f"{repo}/{rpms[name]}" for name in missing]
    srpms = {name: repodata.sourcerpm(plan.known[locked[name].sha256][0]) for name in names if name not in missing}
    with activity(f"getting their sources, for {source}", log=True) as on_line:
        srpms |= _srpm_names(project, [name for name in names if not srpms.get(name)])
        new = {
            srpm: _nevra(locked, name)
            for name, srpm in srpms.items()
            if not plan.has(source, srpm) and not plan.reuse(source, srpm, plan.sources.get(srpm, ""))
        }
        cache = _download(project, new, on_line, source=True)
        (plan.dest / source).mkdir(parents=True, exist_ok=True)
        for srpm in new:
            link(cache / srpm, plan.dest / source / srpm)
            added.append(f"{source}/{srpm}")
    check_signatures(project, plan.dest, added, what="upstream packages")


def snapshot(
    platforms: list[Project], dest: Path, *, signer: Signer | None, storage: Remote | None, build: bool
) -> tuple[list[tuple[Project, dict[str, str]]], dict[str, tuple[str, str]]]:
    """Make the snapshot of the `platforms` in `dest`, and return them with the keys of their packages, and the
    packages of `storage`, by path in the snapshot, with their paths there and SHA-256: they are not in `dest`, see
    materialize().

    Our packages are built or fetched first, but only fetched for the platforms the host emulates: with `storage`, only
    the builds it misses and those they need, as the snapshot copies those it has, like the packages of its latest
    snapshot and of its upstream copy, whose metadata are their own. With `build`, the snapshot has the build
    repository too. Each binary must be installable alone, from the binaries of its architecture, checked with the
    metadata only. Our source packages are those of the first platform: those of the others, built from the same
    sources, differ by their build times. With a `signer`, our packages and the metadata of the repositories are
    signed, and the public key is in `dest`.
    """
    project = platforms[0]
    fresh_dir(project, dest)
    plan = _Plan(project, dest, *(_with_copy(platforms, storage, signer) if storage else ({}, {})))
    built = []
    for platform in platforms:
        keys = _built(platform, fetch="needed" if storage else "all")
        arch = f"{REPO}/{platform.arch}"
        _add_ours(plan, platform, keys, signer=signer, storage=storage)
        _add_stored_upstream(plan, platform, published(platform), arch, f"{REPO}/Source")
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
    """Download to `dest` the packages of `repo` the storage has, `files`, as snapshot() returns them."""
    wanted = {f: stored for f, stored in files.items() if f.startswith(f"{repo}/") and not (dest / f).exists()}

    def fetch(f: str) -> None:
        path, digest = wanted[f]
        _put(storage, *path.rsplit("/", 1), dest / f, digest)

    with activity(f"downloading the packages of {repo}", log=True), ThreadPoolExecutor(remote.REQUESTS) as pool:
        list(pool.map(fetch, wanted))
