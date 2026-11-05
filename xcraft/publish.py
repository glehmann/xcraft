"""Publishing: snapshots of the distribution, made of our builds and the upstream packages they need.

A snapshot covers all the architectures, like the repositories of XCP-ng 8.3: its repository ``base/`` has the
binaries of each architecture, self-contained, in ``base/<arch>/``, and the sources of all of them in
``base/Source/``. It is published as ``repos/<release>/<stage>/<date>[.<n>]/`` in the remote storage, and copied as
``repos/<release>/<stage>/latest/``, see xcraft.upload. All snapshots are kept: the storage stores identical files
once, and files already in the storage are copied there rather than uploaded.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from rich.markup import escape

from xcraft import build as builder
from xcraft import remote, repodata, upstream_copy
from xcraft.cache import BuildError, build_path, cached_build
from xcraft.container import BUILDENV, buildenv_hash, run, run_natively
from xcraft.display import activity, console
from xcraft.localrepo import extra_rpms, fresh_dir, link
from xcraft.lock import load_lock
from xcraft.pinned import pins
from xcraft.project import INTERNAL
from xcraft.resolution import prepare, published, resolve
from xcraft.signing import Signer, check_signatures, dev_signer
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


def trusted_key(project: Project, signer: Signer | None) -> Signer | None:
    """Return the key the trees of the storage are trusted signed with, besides those of xcraft.toml: the one signing
    the snapshot, or the developer's.
    """
    return signer or dev_signer(project)


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
    with project.tmp_dir("download") as work:
        if storage := project.config.remote.get("main"):
            latest, directory = snapshot_tree(project, "repos", "latest"), "Source" if source else project.arch
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
    with project.tmp_dir("srpms") as work:
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


def stored_path(project: Project, signer: Signer | None, name: str, key: str) -> str:
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
    with project.tmp_dir("snapshot") as work:
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


def snapshot_tree(project: Project, kind: str, name: str) -> str:
    """Return the path of a snapshot in the storage, of its repositories or its ISOs: `kind` is repos or isos."""
    return f"{kind}/{project.config.publish.release}/{project.config.publish.stage}/{name}"


def _built(project: Project, *, fetch: Fetch, dev: str | None) -> dict[str, str]:
    """Build or fetch our packages, but only fetch them on the platforms the host emulates, and return their keys: with
    the development builds of the identifier `dev`, if any.
    """
    prepare(project)
    results, keys, _ = builder.build(
        project, sorted(project.packages), build_missing=project.native, fetch=fetch, dev=dev
    )
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


class Stored(NamedTuple):
    """A file of the remote storage: its path there, and its SHA-256."""

    path: str
    digest: str


@dataclass
class _Assembler:
    """A snapshot, signed by `signer`, if any, copying the packages `storage` has, if any: the packages the storage
    misses, all without one, in its directory, and the metadata of all of them.
    """

    project: Project
    dest: Path
    signer: Signer | None
    storage: Remote | None
    known: dict[str, tuple[Entry, str]]
    """The metadata of the packages of the latest snapshot and of the upstream copy, by SHA-256, with their paths in
    the storage."""
    sources: dict[str, str]
    """The SHA-256 of the source packages of the latest snapshot and of the upstream copy, by file name."""
    stored: dict[str, dict[str, Stored]] = field(default_factory=dict)
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
        self.stored.setdefault(repo, {})[name] = Stored(path or self.known[digest][1], digest)
        return True

    def index(self, repo: str) -> Path:
        """Write the metadata of `repo`, signed by `signer`, if any, and return its directory."""
        path = self.dest / repo
        path.mkdir(parents=True, exist_ok=True)
        entries = {name: self.known[stored.digest][0] for name, stored in self.stored.get(repo, {}).items()}
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
        if self.signer:
            self.signer.sign_file(path / "repodata/repomd.xml")
        return path

    def files(self) -> dict[str, Stored]:
        """Return the packages the storage has, by path in the snapshot, with their paths there and SHA-256."""
        return {f"{repo}/{name}": stored for repo, files in self.stored.items() for name, stored in files.items()}

    def add_ours(self, project: Project, keys: dict[str, str]) -> None:
        """Add our packages, with their `keys`, to the base repository of the platform of `project`, and their
        sources.
        """
        arch = f"{REPO}/{project.arch}"
        for repo in (arch, f"{REPO}/Source"):
            (self.dest / repo).mkdir(parents=True, exist_ok=True)
        local = []
        with activity(
            f"signing our packages for {project.arch}" if self.signer else "getting our packages", log=True
        ) as on_line:
            if storage := self.storage:  # their manifests, checked once, concurrently
                trusted = trusted_key(project, self.signer)
                trees = [stored_path(project, self.signer, n, k) for n, k in keys.items()]
                with ThreadPoolExecutor(remote.REQUESTS) as pool:
                    list(pool.map(lambda tree: remote.read_manifest(storage, tree, trusted=trusted), trees))
            for name in sorted(keys):
                on_line(name)
                local += self._add_build(project, name, keys[name])
        # those of the storage are in trees it trusts: checked when signed
        if self.signer:
            check_signatures(project, self.dest, local, what="packages of ours", public_key=self.signer.public_key())
        for rpm in extra_rpms(project):  # neither ours nor from the repositories: as they are
            if not self.reuse(arch, rpm.name, sha256_of(rpm)):
                link(rpm, self.dest / arch / rpm.name)

    def _add_build(self, project: Project, name: str, key: str) -> list[str]:
        """Add the build of `name` with `key`, of the platform of `project`, and return the files put in the directory
        of the snapshot: those the storage misses.
        """
        repos = {"RPMS": f"{REPO}/{project.arch}", "SRPMS": f"{REPO}/Source"}
        tree, files = self._build_files(project, name, key)
        local = []
        for path, digest in sorted(files.items()):
            repo, file = repos.get(path.split("/")[0]), Path(path).name
            if not repo or (repo == repos["SRPMS"] and self.has(repo, file)):
                continue
            at = tree / path if isinstance(tree, Path) else f"{tree}/{path}"
            if not self.reuse(repo, file, digest, at if isinstance(at, str) else None):
                _put(self.storage, at, self.dest / repo / file, digest)
                local.append(f"{repo}/{file}")
        return local

    def _build_files(self, project: Project, name: str, key: str) -> tuple[Path | str, dict[str, str]]:
        """Return the directory of the files of the build of `name` with `key`, signed by `signer`, if any, in the
        storage, or the local one when there is no storage, or it misses it: signed now, and pushed there; with its
        files, by path in it, and their SHA-256.
        """
        if self.storage:
            path, trusted = stored_path(project, self.signer, name, key), trusted_key(project, self.signer)
            root, files = remote.read_tree(self.storage, path, trusted=trusted)
            if files:
                return root, files
        if not cached_build(project, name, key).exists() and not builder.fetch_build(project, name, key):
            msg = f"the build of {name} is neither cached nor in the remote storage"
            raise BuildError(msg)
        local = (
            _signed_build(project, self.signer, name, key, storage=self.storage)
            if self.signer
            else cached_build(project, name, key)
        )
        return local, {p.relative_to(local).as_posix(): sha256_of(p) for p in local.rglob("*.rpm")}

    def add_upstream(self, project: Project, names: list[str], repo: str, source: str) -> None:
        """Add the upstream packages `names` to `repo`, and their sources to `source`, checking the signatures of
        those the storage misses: those it has were when added.
        """
        locked = pins(project)
        rpms = {name: _rpm_name(locked, name) for name in names}
        missing = [name for name in names if not self.reuse(repo, rpms[name], locked[name].sha256)]
        with activity(f"getting the upstream packages for {repo}", log=True) as on_line:
            (self.dest / repo).mkdir(parents=True, exist_ok=True)
            for rpm in _upstream_rpms(project, missing, on_line):
                link(rpm, self.dest / repo / rpm.name)
        added = [f"{repo}/{rpms[name]}" for name in missing]
        srpms = {name: repodata.sourcerpm(self.known[locked[name].sha256][0]) for name in names if name not in missing}
        with activity(f"getting their sources, for {source}", log=True) as on_line:
            srpms |= _srpm_names(project, [name for name in names if not srpms.get(name)])
            new = {
                srpm: _nevra(locked, name)
                for name, srpm in srpms.items()
                if not self.has(source, srpm) and not self.reuse(source, srpm, self.sources.get(srpm, ""))
            }
            cache = _download(project, new, on_line, source=True)
            (self.dest / source).mkdir(parents=True, exist_ok=True)
            for srpm in new:
                link(cache / srpm, self.dest / source / srpm)
                added.append(f"{source}/{srpm}")
        check_signatures(project, self.dest, added, what="upstream packages")


def previous(
    project: Project, storage: Remote, signer: Signer | None
) -> tuple[dict[str, tuple[Entry, str]], dict[str, str]]:
    """Return the metadata of the packages of the latest snapshot, by SHA-256, with their paths in the storage, and
    the SHA-256 of its source packages, by file name.

    Only the metadata matching its trusted manifest is read, for the packages with the SHA-256 of the manifest: none
    of a repository whose metadata Python cannot decompress, like zstd before Python 3.14.
    """
    latest = snapshot_tree(project, "repos", "latest")
    manifest = remote.read_manifest(storage, latest, trusted=trusted_key(project, signer))
    known = {}
    with project.tmp_dir("previous") as work, activity("reading the metadata of the latest snapshot", log=True):

        def fetch(path: str) -> bool:
            # of the manifest only, whose paths stay in the tree
            return (
                path in manifest
                and remote.download(storage, f"{latest}/{path}", work / path)
                and (sha256_of(work / path) == manifest[path])
            )

        for repo in (f.removesuffix("/repodata/repomd.xml") for f in manifest if f.endswith("/repodata/repomd.xml")):
            if _fetch_metadata(work, repo, fetch):
                known |= {
                    digest: (entry, f"{latest}/{path}")
                    for digest, entry in (repodata.read(work / repo) or {}).items()
                    if manifest.get(path := f"{repo}/{repodata.location(entry)}") == digest
                }
    sources = {
        Path(f).name: digest for f, digest in manifest.items() if f.startswith((f"{REPO}/Source/", f"{BUILD}/Source/"))
    }
    return known, sources


def _fetch_metadata(work: Path, repo: str, fetch: Callable[[str], bool]) -> bool:
    """Download the metadata of `repo` to `work` with `fetch`, and return whether it got it all."""
    if not fetch(repomd := f"{repo}/repodata/repomd.xml"):
        return False
    hrefs = repodata.files((work / repomd).read_bytes()).values()
    with ThreadPoolExecutor(remote.REQUESTS) as pool:
        return all(pool.map(fetch, [f"{repo}/{href}" for href in hrefs]))


def _with_copy(
    platforms: list[Project], storage: Remote, signer: Signer | None
) -> tuple[dict[str, tuple[Entry, str]], dict[str, str]]:
    """Return the packages of the latest snapshot, like previous(), and those of the upstream copy of the storage, for
    the architectures of `platforms`: copied from there, without being uploaded.
    """
    known, sources = upstream_copy.stored(platforms[0], storage, [p.arch for p in platforms])
    latest, latest_sources = previous(platforms[0], storage, signer)
    return known | latest, sources | latest_sources


def _put(storage: Remote | None, source: Path | str, dest: Path, digest: str) -> None:
    """Put the local file `source`, or the one of `storage`, as `dest`, checking its SHA-256."""
    if isinstance(source, Path):
        link(source, dest)
    elif not (storage and remote.download(storage, source, dest)) or sha256_of(dest) != digest:
        msg = f"{source} is missing or corrupted in the storage"
        raise BuildError(msg)


def snapshot(
    platforms: list[Project],
    dest: Path,
    *,
    signer: Signer | None,
    storage: Remote | None,
    build: bool,
    dev: str | None = None,
) -> tuple[list[tuple[Project, dict[str, str]]], dict[str, Stored]]:
    """Make the snapshot of the `platforms` in `dest`, and return them with the keys of their packages, and the
    packages of `storage`, by path in the snapshot, with their paths there and SHA-256: they are not in `dest`, see
    materialize().

    Our packages are built or fetched first, but only fetched for the platforms the host emulates: with `storage`, only
    the builds it misses and those they need, as the snapshot copies those it has, like the packages of its latest
    snapshot and of its upstream copy, whose metadata are their own. With `build`, the snapshot has the build
    repository too. Each binary must be installable alone, from the binaries of its architecture, checked with the
    metadata only. Our source packages are those of the first platform: those of the others, built from the same
    sources, differ by their build times. With a `signer`, our packages and the metadata of the repositories are
    signed, and the public key is in `dest`. With `dev`, the identifier of the development builds, those of the packages
    whose build ID is not the main branch's are, for local snapshots, without `storage`.
    """
    project = platforms[0]
    fresh_dir(project, dest)
    assembler = _Assembler(
        project, dest, signer, storage, *(_with_copy(platforms, storage, signer) if storage else ({}, {}))
    )
    built = []
    for platform in platforms:
        keys = _built(platform, fetch="needed" if storage else "all", dev=dev)
        arch = f"{REPO}/{platform.arch}"
        assembler.add_ours(platform, keys)
        assembler.add_upstream(platform, published(platform), arch, f"{REPO}/Source")
        _check_installable(platform, [assembler.index(arch)])
        if build:
            assembler.add_upstream(
                platform, build_dependencies(platform), f"{BUILD}/{platform.arch}", f"{BUILD}/Source"
            )
            _check_installable(platform, [assembler.dest / arch, assembler.index(f"{BUILD}/{platform.arch}")])
        built.append((platform, keys))
    for repo in (REPO, BUILD) if build else (REPO,):
        assembler.index(f"{repo}/Source")
    if signer:
        (dest / KEY_FILE).write_text(signer.public_key())
    _summary(dest, built, build=build)
    return built, assembler.files()


def materialize(storage: Remote, dest: Path, files: dict[str, Stored], repo: str) -> None:
    """Download to `dest` the packages of `repo` the storage has, `files`, as snapshot() returns them."""
    wanted = {f: stored for f, stored in files.items() if f.startswith(f"{repo}/") and not (dest / f).exists()}

    def fetch(f: str) -> None:
        _put(storage, wanted[f].path, dest / f, wanted[f].digest)

    with activity(f"downloading the packages of {repo}", log=True), ThreadPoolExecutor(remote.REQUESTS) as pool:
        list(pool.map(fetch, wanted))
