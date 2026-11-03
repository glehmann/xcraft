"""Our copy of the pinned upstream packages, in the remote storage, for when their repositories drop them: see
docs/upstream-mirror-design.md.

A flat repository by architecture, in ``.xcraft/upstream/<arch>/``, and one of their source packages, in
``.xcraft/upstream/Source/``, append-only: every package copied stays, the lock deciding which versions are used, as
with the upstream repositories. Their metadata is updated with xcraft.repodata, from its own entries and those of the
packages copied, without downloading the packages it has. The commands getting the pins, the resolution and the pin
downloads, have it as a repository, see container.run_natively(); xcraft publish copies from it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from xcraft import pinned, remote, repodata
from xcraft.container import ContainerError, run_natively
from xcraft.display import activity
from xcraft.localrepo import link
from xcraft.lock import load_lock
from xcraft.project import INTERNAL

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.lock import Pin
    from xcraft.project import Project, Remote
    from xcraft.repodata import Entry

PREFIX = f"{INTERNAL}/upstream"
SOURCE = "Source"

type Copies = dict[str, tuple[Path | str, Entry | None]]
"""The packages to copy, by file name: a local RPM, or a path of the storage with its metadata entry."""


class UpstreamError(RuntimeError):
    """Raised when the copy cannot be read."""


def repo_file(project: Project) -> str:
    """Return the definition of the copies of the storages, for the architecture of `project`: skipped when they cannot
    be reached, like offline. The pins are checked against their SHA-256, and the resolution installs nothing.
    """
    return "".join(
        f"[xcraft-upstream-{name}]\nname=xcraft upstream copy of {name}\n"
        f"baseurl={storage.url.rstrip('/')}/{PREFIX}/{project.arch}/\n"
        "gpgcheck=0\nskip_if_unavailable=1\nmetadata_expire=0\n"
        for name in ("main", "dev")
        if (storage := project.config.remote.get(name))
    )


def read(storage: Remote, directory: str, work: Path) -> tuple[dict[str, Entry], list[str]]:
    """Return the metadata entries of the packages of the copy `directory`, by SHA-256, read in `work`, and the paths
    of its metadata files: nothing without metadata.
    """
    prefix = f"{PREFIX}/{directory}"
    if not remote.download(storage, f"{prefix}/repodata/repomd.xml", work / "repodata/repomd.xml"):
        return {}, []
    hrefs = list(repodata.files((work / "repodata/repomd.xml").read_bytes()).values())
    with ThreadPoolExecutor(remote.REQUESTS) as pool:
        if not all(pool.map(lambda href: remote.download(storage, f"{prefix}/{href}", work / href), hrefs)):
            msg = f"{prefix}/repodata/repomd.xml lists metadata the storage does not have"
            raise UpstreamError(msg)
    if (entries := repodata.read(work)) is None:
        msg = f"cannot read the metadata of {prefix}"
        raise UpstreamError(msg)
    return entries, hrefs


def stored(project: Project, storage: Remote, arches: list[str]) -> tuple[dict[str, tuple[Entry, str]], dict[str, str]]:
    """Return the packages of the copies of `arches` and of the source packages, by SHA-256, with their metadata and
    their paths in `storage`, and the SHA-256 of the source packages, by file name, like publish.previous().
    """
    known, sources = {}, {}
    with project.work("upstream-copy") as work:
        for directory in [*arches, SOURCE]:
            for digest, entry in read(storage, directory, work / directory)[0].items():
                known[digest] = (entry, f"{PREFIX}/{directory}/{(name := repodata.location(entry))}")
                if directory == SOURCE:
                    sources[name] = digest
    return known, sources


def push(project: Project, storage: Remote) -> list[str]:
    """Copy to `storage` the pinned packages of the architecture of `project` its copy misses, and the source packages
    of those the snapshots publish, from the store of the pinned RPMs of the machine, the latest snapshot, or else the
    upstream repositories, and return the problems: the packages found nowhere, and those of the file names of others.
    """
    from xcraft import publish  # noqa: PLC0415, which uses this module

    pins = pinned.pins(project)
    snapshot: list[tuple[dict[str, tuple[Entry, str]], dict[str, str]]] = []

    def latest() -> tuple[dict[str, tuple[Entry, str]], dict[str, str]]:
        if not snapshot:  # read once, when needed
            snapshot.append(publish.previous(project, storage, None))
        return snapshot[0]

    with project.work("upstream") as work:
        entries, hrefs = read(storage, project.arch, work / "metadata")
        wanted, problems = _wanted(entries, {pinned.file_name(n, p): (n, p) for n, p in pins.items()})
        copies: Copies = {f: (rpm, None) for f, (n, p) in wanted.items() if (rpm := pinned.stored(n, p))}
        if left := [f for f in wanted if f not in copies]:
            known = latest()[0]
            copies |= {f: (known[s][1], known[s][0]) for f in left if (s := wanted[f][1].sha256) in known}
        if left := {n: p for f, (n, p) in wanted.items() if f not in copies}:
            pinned.download(project, left)
            copies |= {
                f: (rpm, None) for f, (n, p) in wanted.items() if f not in copies and (rpm := pinned.stored(n, p))
            }
        problems += [f"{f}: in no repository, nor in the store of the pinned RPMs" for f in wanted if f not in copies]
        new = _update(project, storage, project.arch, work, entries, hrefs, copies)
        entries |= {wanted[f][1].sha256: entry for f, entry in new.items()}
        # the source packages of those the snapshots publish, from their metadata
        binaries = {pins[n].sha256: f"{n}-{pins[n].evr}.{pins[n].arch}" for n in _published(project)}
        srpms = {repodata.sourcerpm(e): binaries[s] for s, e in entries.items() if s in binaries}
        entries, hrefs = read(storage, SOURCE, work / "source-metadata")
        have = {repodata.location(e) for e in entries.values()}
        if missing := {f: nevra for f, nevra in srpms.items() if f not in have}:
            known, sources = latest()
            copies = {f: (known[s][1], known[s][0]) for f in missing if (s := sources.get(f, "")) in known}
            if left := {f: nevra for f, nevra in missing.items() if f not in copies}:
                try:
                    cache = publish.download_sources(project, left)
                except ContainerError as e:
                    problems.append(f"source packages: {e}")
                else:
                    copies |= {f: (cache / f, None) for f in left if (cache / f).exists()}
            problems += [f"{f}: in no repository, nor in the latest snapshot" for f in missing if f not in copies]
            _update(project, storage, SOURCE, work, entries, hrefs, copies)
    return problems


def _published(project: Project) -> set[str]:
    """Return the pinned packages the snapshots publish, of the lock: those of the published repositories, and of the
    build environment and the build roots, with the build repository, see publish.build_dependencies().
    """
    lock, arch = load_lock(project), project.arch
    names = {*lock.runtime.get(arch, [])}
    if project.config.publish.build:
        names |= {*lock.image.get(arch, []), *(n for names in lock.roots.of(arch).values() for n in names)}
    return names


def _wanted(
    entries: dict[str, Entry], files: dict[str, tuple[str, Pin]]
) -> tuple[dict[str, tuple[str, Pin]], list[str]]:
    """Return those of the pinned packages `files`, by file name, the copy of `entries` misses, and the problems: the
    file names it has with other contents.
    """
    names = {repodata.location(e): digest for digest, e in entries.items()}
    missing = {f: (n, p) for f, (n, p) in files.items() if p.sha256 not in entries}
    clashing = [f for f in missing if f in names]
    return (
        {f: missing[f] for f in missing if f not in clashing},
        [f"{f}: in the copy with the SHA-256 {names[f]}, not {missing[f][1].sha256}" for f in clashing],
    )


def _update(
    project: Project,
    storage: Remote,
    directory: str,
    work: Path,
    entries: dict[str, Entry],
    hrefs: list[str],
    copies: Copies,
) -> dict[str, Entry]:
    """Add the packages `copies` to the copy `directory`, of metadata `entries` and files `hrefs`, and return their
    metadata entries, by file name: the new metadata files first, then repomd.xml, and the metadata files of before
    the previous update deleted.
    """
    if not copies:
        return {}
    prefix, local = f"{PREFIX}/{directory}", work / f"new-{directory}"
    local.mkdir()
    for f, (src, _) in copies.items():
        if not isinstance(src, str):
            link(src, local / f)
    new = {f: entry for f, (_, entry) in copies.items() if entry}
    if len(new) < len(copies):
        run_natively(
            project.on_host(),
            "upstream-repo",
            ["bash", "-c", "dnf -q -y install createrepo_c && createrepo_c -q --general-compress-type=gz /repo"],
            volumes=[f"{local}:/repo"],
            show=f"indexing the new packages of the upstream copy of {directory}",
        )
        made = {repodata.location(e): e for e in (repodata.read(local) or {}).values()}
        new |= {f: made[f] for f in copies if f not in new}
    with activity(f"copying {len(copies)} packages to the upstream copy of {directory}"):
        remote.transfer(storage, {f"{prefix}/{f}": src for f, (src, _) in copies.items()})
        metadata = work / f"repo-{directory}"
        repodata.write(metadata, {repodata.location(e): e for e in entries.values()} | new)
        written = sorted(p.relative_to(metadata).as_posix() for p in (metadata / "repodata").iterdir())
        remote.transfer(storage, {f"{prefix}/{p}": metadata / p for p in written if p != "repodata/repomd.xml"})
        remote.upload(storage, f"{prefix}/repodata/repomd.xml", metadata / "repodata/repomd.xml")
        keep = {*hrefs, *written}
        if stale := [p for p in remote.listing(storage, f"{prefix}/repodata/") if f"repodata/{p}" not in keep]:
            remote.delete(storage, [f"{prefix}/repodata/{p}" for p in stale])
    return new
