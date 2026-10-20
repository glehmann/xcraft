"""Publishing: snapshots of the distribution, made of our builds and the upstream packages they need.

A snapshot is a self-contained repository, published as
``repos/<release>/<date>[.<n>]/<arch>/`` in the remote storage, and copied as
``repos/<release>/latest/<arch>/``. All snapshots are kept: the storage stores
identical files once, and the files already in the storage are copied there
rather than uploaded.
"""

from __future__ import annotations

import contextlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from rich.markup import escape

from xcraft import remote
from xcraft.build import BuildError, build_path, builds_repo, cached_build
from xcraft.container import DNF_LOCK, run
from xcraft.display import activity, console
from xcraft.localrepo import OUTPUT_MARKER, LocalRepo, fresh_dir, link
from xcraft.lock import Lock, Locked, load_lock, save_lock
from xcraft.signing import Signer, check_signatures
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable

    from xcraft.project import Project, Remote

KEY_FILE = "RPM-GPG-KEY"
"""The public key of the signed snapshots, in them."""


def resolve_runtime(project: Project, keys: dict[str, str]) -> list[str]:
    """Add to the lock the upstream packages our builds need, and the extra ones, and return their names."""
    lock = load_lock(project)
    repo = builds_repo(project, keys)
    with project.work("runtime") as work:
        (work / "input.json").write_text(
            json.dumps(
                {
                    "locked": {name: [p.evr, p.arch] for name, p in lock.packages.items()},
                    "extra": project.config.publish.extra,
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
    needed = {name: Locked.model_validate(p) for name, p in output["packages"].items()}
    lock.merge(needed)
    lock.runtime = sorted(needed)
    save_lock(project, lock)
    return lock.runtime


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
                    path = f"{_snapshot_path(project, 'latest')}/{'source' if source else 'os'}/{f}"
                    remote.download(storage, path, work / f)
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


def _nevra(lock: Lock, name: str) -> str:
    return f"{name}-{lock.packages[name].evr}.{lock.packages[name].arch}"


def _upstream_rpms(project: Project, names: list[str], on_line: Callable[[str], None]) -> list[Path]:
    """Return the RPMs of locked upstream packages: from the cache, the latest snapshot, or the repositories."""
    lock = load_lock(project)
    rpms = {name: f"{name}-{lock.packages[name].evr.split(':')[-1]}.{lock.packages[name].arch}.rpm" for name in names}
    cache = _download(project, {rpms[name]: _nevra(lock, name) for name in names}, on_line, source=False, volumes=[])
    for name in names:
        if sha256_of(cache / rpms[name]) != lock.packages[name].sha256:
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
    lock = load_lock(project)
    nevras = {name: _nevra(lock, name) for name in names}
    with project.work("srpms") as work:
        # only for these downloads, not to change the build environment
        (work / "sources.repo").write_text(
            "".join(
                f"[{name}-source]\nname={name} sources\nbaseurl={repo.source_baseurl}\nenabled=0\ngpgcheck=0\n"
                for name, repo in project.config.repos.items()
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
    return f"signed/{signer.key}/{project.config.arch}/{name}/{key}"


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


def assemble(project: Project, keys: dict[str, str], dest: Path, *, signer: Signer | None, push: bool = False) -> None:
    """Make `dest` the repositories of a snapshot, os/ and source/, and check each binary can be installed alone.

    It has the builds of our packages with `keys`.

    With a `signer`, our packages and the repositories' metadata are signed, and the public key is in `dest`.
    """
    lock = load_lock(project)
    fresh_dir(project, dest)
    binaries, sources = LocalRepo(project, dest / "os"), LocalRepo(project, dest / "source")
    for repo in (binaries, sources):
        repo.reset(extra_upstream=False)
    ours = []
    with activity("signing our packages" if signer else "getting our packages") as on_line:
        for name in sorted(keys):
            on_line(name)
            build = (
                _signed_build(project, signer, name, keys[name], push=push)
                if signer
                else cached_build(project, name, keys[name])
            )
            binaries.add(build)
            ours += [f"os/{rpm.name}" for rpm in (build / "RPMS").rglob("*.rpm")]
            for srpm in (build / "SRPMS").glob("*.src.rpm"):
                link(srpm, sources.path / srpm.name)
                ours.append(f"source/{srpm.name}")
    if signer:
        check_signatures(project, dest, ours, what="packages of ours", public_key=signer.public_key())
    with activity("getting the upstream packages") as on_line:
        upstream = [f"os/{rpm.name}" for rpm in _upstream_rpms(project, lock.runtime, on_line)]
        for rpm in upstream:
            link(project.state / "cache/rpms" / Path(rpm).name, dest / rpm)
    with activity("getting the sources of the upstream packages") as on_line:
        for srpm in _upstream_srpms(project, lock.runtime, on_line):
            if not (sources.path / srpm.name).exists():  # the source of several binaries
                link(srpm, sources.path / srpm.name)
                upstream.append(f"source/{srpm.name}")
    check_signatures(project, dest, upstream, what="upstream packages")
    binaries.index()
    sources.index()
    if signer:
        for repo in (binaries, sources):
            signer.sign_file(repo.path / "repodata/repomd.xml")
        (dest / KEY_FILE).write_text(signer.public_key())
    _check_installable(project, binaries.path)


def _check_installable(project: Project, repo: Path) -> None:
    """Check each package of the repository `repo` can be installed from it alone."""
    with project.work("snapshot") as work:
        run(
            project,
            "snapshot",
            f"{DNF_LOCK} python3 /xcraft/installable.py /snapshot /work/errors.json",
            volumes=[f"{repo}:/snapshot:ro", f"{work}:/work"],
            show="checking that the snapshot is self-contained",
        )
        errors = json.loads((work / "errors.json").read_text())
    if errors:
        for package, error in sorted(errors.items()):
            console.print(f"[red]✗[/] {escape(f'{package}: {error}')}")
        msg = f"the snapshot is not self-contained: {len(errors)} packages cannot be installed from it"
        raise BuildError(msg)


def _snapshot_path(project: Project, name: str) -> str:
    """Return the path of a snapshot in the storage, with its os/ and source/ repositories."""
    return f"repos/{project.config.publish.release}/{name}/{project.config.arch}"


def snapshot_name(project: Project, storage: Remote) -> str:
    """Return the name of a new snapshot: today's date, with the first suffix not used for this architecture."""
    day = datetime.now(UTC).date().isoformat()
    for name in (day, *(f"{day}.{n}" for n in range(2, 1000))):
        if not remote.exists(storage, f"{_snapshot_path(project, name)}/os/repodata/repomd.xml"):
            return name
    msg = f"too many snapshots on {day}"
    raise BuildError(msg)


def _delete_stale(storage: Remote, prefix: str, keep: set[str]) -> None:
    """Delete the files under `prefix` but those of `keep`, and the pages of the directories, written again after."""
    stale = [p for p in remote.listing(storage, f"{prefix}/") if p.removeprefix(f"{prefix}/") not in keep]
    remote.delete(storage, [p for p in stale if Path(p).name != remote.INDEX])


def upload_snapshot(project: Project, keys: dict[str, str], src: Path, name: str, *, signer: Signer | None) -> None:
    """Upload the snapshot in `src` as `name`, and make it the latest one.

    The packages already in the storage, in the builds with `keys` or the latest snapshot, are copied there.
    """
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
    for package, key in keys.items():
        tree = _signed_path(project, signer, package, key) if signer else build_path(project, package, key)
        for path, digest in remote.read_manifest(storage, tree).items():
            repo = {"RPMS": "os", "SRPMS": "source"}.get(path.split("/")[0])
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
        signed = [f"signed/{signer.key}/{project.config.arch}/"] if signer else []
        remote.update_pages(storage, [f"{snapshot}/", f"{latest}/", *signed])
    console.print(f"[bold green]✓[/] published {snapshot}, and as {latest}")
