"""The copy of the latest snapshots of a stage, for the mirrors to sync from, see docs/publishing-layout-design.md.

The copy has the paths of the storage, `<dir>/<kind>/<release>/<stage>/latest`, each a symlink to a version of its
own, `.latest-<generation>`, made from the public endpoint of the storage, its signed manifest checked first, then
switched at once: an rsync session sees one complete tree. A file has the date of the storage, its Last-Modified, and
fixed modes, so a copy made again from scratch is the same for rsync, which compares sizes and dates: the mirrors
transfer nothing again. A file of the previous version, of the same path, SHA-256 and date, is hard linked rather than
downloaded.
"""

from __future__ import annotations

import os
import shutil
from typing import TYPE_CHECKING, NoReturn

from xcraft import remote
from xcraft.cache import BuildError
from xcraft.process import thread_pool
from xcraft.publish import snapshot_tree
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    import datetime as dt
    from pathlib import Path

    from xcraft.project import Project, Remote
    from xcraft.signing import Signer

KINDS = ("repos", "isos")
"""The latest snapshots of a stage the copy has."""
_VERSION = ".latest-"
"""The prefix of the versions of a latest snapshot, next to its symlink."""


def _local(storage: Remote, tree: str, link: Path, trusted: Signer | None) -> tuple[Path, dict[str, str]] | None:
    """Return the version of the copy of `tree`, at `link`, with the SHA-256 of its files, when its manifest is
    signed by a key the storage trusts.
    """
    if not (link.is_symlink() and (link / remote.MANIFEST).exists()):
        return None
    checked = remote.checked_manifest(storage, tree, (link / remote.MANIFEST).read_bytes(), trusted=trusted)
    return (link.resolve(), checked[1]) if checked else None


def _missing(tree: str, f: str) -> NoReturn:
    msg = f"{tree}/{f} is missing in the storage"
    raise BuildError(msg)


def _date(path: Path, date: dt.datetime) -> None:
    """Give `path` the date `date`, and the mode of the copy."""
    path.chmod(0o644)
    os.utime(path, (date.timestamp(), date.timestamp()))


def _fetch(storage: Remote, path: str, dest: Path, digest: str | None, date: dt.datetime) -> None:
    """Download `path` to `dest`, checking its SHA-256 `digest`, if given, with its `date`."""
    if not remote.download(storage, path, dest) or (digest and sha256_of(dest) != digest):
        msg = f"{path} is missing or corrupted in the storage"
        raise BuildError(msg)
    _date(dest, date)


def _copy(storage: Remote, tree: str, link: Path, trusted: Signer | None) -> bool:
    """Make the copy of the latest snapshot `tree` at `link`, and return whether it changed.

    It raises when a publication changed the snapshot meanwhile, its manifest dated otherwise after the copy than
    before, to the second: the copy is not switched to, and the next run makes it again.
    """
    dated = remote.modified(storage, f"{tree}/{remote.MANIFEST}")
    root, files = remote.read_tree(storage, tree, trusted=trusted)
    if not files:
        return False
    version = link.parent / f"{_VERSION}{remote.generation(tree, files).rsplit('/', 1)[1]}"
    if link.is_symlink() and link.resolve() == version.resolve() and version.exists():
        return False
    previous = _local(storage, tree, link, trusted)
    sources = {f: f"{root}/{f}" for f in files}
    with thread_pool(remote.REQUESTS) as pool:
        dates = dict(zip(sources, pool.map(lambda f: remote.modified(storage, sources[f]), sources), strict=True))
    part = version.with_name(f"{version.name}.part")
    shutil.rmtree(part, ignore_errors=True)

    def copy(f: str) -> None:
        date = dates[f] or _missing(tree, f)
        (part / f).parent.mkdir(parents=True, exist_ok=True)
        old = previous[0] / f if previous else None
        same = previous and old and old.exists() and previous[1].get(f) == files[f]
        if same and old and old.stat().st_mtime == date.timestamp():
            os.link(old, part / f)
        else:
            _fetch(storage, sources[f], part / f, files[f], date)

    with thread_pool(remote.REQUESTS) as pool:
        list(pool.map(copy, files))
    # the manifest checked, not downloaded again: a publication finished meanwhile would give one of other files
    if (text := remote.manifest_bytes(storage, tree)) is None:
        _missing(tree, remote.MANIFEST)
    (part / remote.MANIFEST).write_bytes(text)
    _date(part / remote.MANIFEST, dated or _missing(tree, remote.MANIFEST))
    if remote.modified(storage, f"{tree}/{remote.MANIFEST}") != dated:
        shutil.rmtree(part)
        msg = f"{tree} was published again while it was copied: run again"
        raise BuildError(msg)
    _switch(link, part, version, previous[0] if previous else None)
    return True


def _switch(link: Path, part: Path, version: Path, previous: Path | None) -> None:
    """Make the copy `part` the version `version` of the latest snapshot at `link`, switched at once, then remove the
    versions but the `previous` one: an rsync session may read it still.
    """
    for directory in [part, *(p for p in part.rglob("*") if p.is_dir())]:
        directory.chmod(0o755)
    if version.exists():
        shutil.rmtree(version)
    part.rename(version)
    (swap := link.with_name(f"{link.name}.new")).unlink(missing_ok=True)
    swap.symlink_to(version.name)
    swap.replace(link)
    for old in link.parent.glob(f"{_VERSION}*"):
        if old not in (version, previous) and not old.name.endswith(".part"):
            shutil.rmtree(old)


def mirrored(project: Project, storage: Remote, dest: Path, trusted: Signer | None) -> list[str]:
    """Make the copy of the latest snapshots of the stage of `project` in `dest`, and return those that changed."""
    trees = [snapshot_tree(project, kind, "latest") for kind in KINDS]
    return [tree for tree in trees if _copy(storage, tree, dest / tree, trusted)]


def checked(project: Project, storage: Remote, dest: Path, trusted: Signer | None, *, repair: bool = True) -> list[str]:
    """Return the problems of the copy of the latest snapshots of the stage of `project` in `dest`: its manifests
    not signed by a key the storage trusts, its files missing or damaged, downloaded again unless not `repair`, and
    those the manifests do not have.
    """
    problems = []
    for kind in KINDS:
        tree = snapshot_tree(project, kind, "latest")
        if not (link := dest / tree).is_symlink():
            continue
        if not (local := _local(storage, tree, link, trusted)):
            problems.append(f"{tree}/{remote.MANIFEST}: not signed by a trusted key")
            continue
        version, files = local
        for f, digest in sorted(files.items()):
            if (version / f).is_file() and sha256_of(version / f) == digest:
                continue
            problems.append(f"{tree}/{f}: {'damaged' if (version / f).exists() else 'missing'}")
            if not repair:
                continue
            try:  # a file of its own, not of the hard links of the other versions
                (part := version / f"{f}.part").parent.mkdir(parents=True, exist_ok=True)
                _fetch(
                    storage, f"{tree}/{f}", part, digest, remote.modified(storage, f"{tree}/{f}") or _missing(tree, f)
                )
                part.replace(version / f)
            except BuildError as e:
                problems.append(f"{tree}/{f}: not repaired, {e}: the copy is to make again")
        extra = {p.relative_to(version).as_posix() for p in version.rglob("*") if p.is_file()} - {
            *files,
            remote.MANIFEST,
        }
        problems += [f"{tree}/{f}: not in the manifest" for f in sorted(extra)]
    return problems
