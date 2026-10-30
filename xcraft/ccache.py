"""The compiler caches of the packages, shared through the remote storage.

Each package has its own ccache directory, which the builds of CI fill. It is shared as a signed tree,
``.xcraft/ccache/<arch>/<package>/``, with an object by entry, at its path in the directory, rather than read from the
storage by ccache itself, as the builds run without the network: a build without a local compiler cache gets it first.
Only what one side has and the other has not is transferred, see push() and fetch(). The entries are compressed again
with a higher level before being shared. See docs/ccache-sync-design.md.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from rich.markup import escape

from xcraft import remote
from xcraft.container import run
from xcraft.display import activity, console
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project, Remote
    from xcraft.signing import Signer


LEVEL = 9
"""The zstd level of the entries of the shared compiler caches: higher than the one of the builds, ccache's, which
favors their speed, for smaller entries to transfer. For the kernel, on 8 CPUs, from 2,046 MiB at level 1: 1,731 MiB
in 20s at level 9, 1,720 MiB in 36s at 12, 1,702 MiB in 77s at 15, and 1,538 MiB in 224s at 19."""
_LAST_BUILD = "xcraft-last-build"
"""When the last successful build of the package started: the entries ccache dates after, as it dates them of their
last use, are those it used or made."""
_LOCAL = {"ccache.conf", "tmp", _LAST_BUILD}
"""What is not shared: the configuration of a cache, its temporary files, and when its last build started."""


def local_path(project: Project, name: str) -> Path:
    """Return the compiler cache of `name`, which its builds use."""
    return project.state / "ccache" / name


def remote_path(project: Project, name: str) -> str:
    """Return the tree of the compiler cache of `name` in the remote storage."""
    return f"{remote.CCACHE}/{project.arch}/{name}"


def note_build(project: Project, name: str, start: float) -> None:
    """Note that the build of `name` started at `start` succeeded: see push()."""
    (local_path(project, name) / _LAST_BUILD).write_text(f"{start}\n")


def _result(entry: str) -> bool:
    """Return whether `entry` is a result: its name is the hash of its inputs, valid whatever its bytes, unlike a ccache
    manifest, which ccache changes in place.
    """  # noqa: D205
    return entry.endswith("R")


def _level(path: Path) -> int:
    """Return the zstd level of the entry at `path`, in the header ccache writes."""
    with path.open("rb") as f:
        return f.read(6)[5]


def push(project: Project, storage: Remote, name: str, *, signer: Signer | None) -> bool:
    """Update the compiler cache of `name` in the storage with the local one, and return whether there is one.

    Only the entries its last successful build used or made are kept, if it was built: not those of the older builds,
    which the local cache keeps. Its statistics neither, which would count them. The local cache is compressed again
    first, with the zstd `LEVEL`, in place, once: ccache keeps the dates of the entries, which tell those the last
    build used, and leaves those compressed so already. Then the entries the storage does not have so are uploaded:
    those it has with other bytes, but the results, which are the same by name. The others of the storage, and those
    of pushes interrupted before their manifest, are deleted after the new manifest.
    """
    if not (cache := local_path(project, name)).is_dir():
        return False
    signer = remote.required(signer)  # before uploading anything
    since = int(float(last.read_text())) if (last := cache / _LAST_BUILD).exists() else 0
    prefix = remote_path(project, name)
    stored = remote.read_manifest(storage, prefix, trusted=signer)
    with project.lock():  # the builds use the cache, which this changes
        used = sorted(
            path.relative_to(cache).as_posix()
            for path in cache.rglob("*")
            if path.is_file()
            and path.relative_to(cache).parts[0] not in _LOCAL
            and path.name != "stats"
            and path.stat().st_mtime >= since
        )
        if any(_level(cache / entry) != LEVEL for entry in used if entry.endswith(("R", "M"))):
            run(
                project,
                "ccache",
                f"CCACHE_DIR=/ccache ccache --recompress {LEVEL}",
                volumes=[f"{cache}:/ccache"],
                show=f"compressing the ccache of {name}",
                network=False,
            )
        manifest = {
            entry: stored[entry] if _result(entry) and entry in stored else sha256_of(cache / entry) for entry in used
        }
        new = [entry for entry in used if manifest[entry] != stored.get(entry)]
        with activity(f"uploading the ccache of {name}") as on_line:
            remote.transfer(
                storage, {f"{prefix}/{entry}": cache / entry for entry in new}, on_line, requests=remote.MANY
            )
            if manifest != stored:
                remote.write_manifest(storage, prefix, manifest, signer=signer)
            stale = [f for f in remote.listing(storage, f"{prefix}/") if f not in manifest and f != remote.MANIFEST]
            remote.delete(storage, [f"{prefix}/{f}" for f in stale])
    return True


def fetch(project: Project, name: str) -> bool:
    """Add to the local compiler cache of `name` the entries of the storage it does not have, and return whether the
    storage has one.
    """  # noqa: D205
    cache = local_path(project, name)

    def wanted(entry: str, digest: str) -> bool:
        # the entries are in the directories of the cache: not the archive of an older xcraft, at the top
        return "/" in entry and (
            not (cache / entry).exists() or (not _result(entry) and sha256_of(cache / entry) != digest)
        )

    start = time.time()
    with activity(f"downloading the ccache of {name}") as on_line:
        found = remote.update_tree(project, remote_path(project, name), cache, wanted, on_line)
    # dated before the last build, which did not use them, to push those it used only
    if (last := cache / _LAST_BUILD).exists():
        before = float(last.read_text()) - 1
        for path in cache.rglob("*"):
            if path.is_file() and path.stat().st_mtime >= start:
                os.utime(path, (before, before))
    return found


def fetch_missing(project: Project, name: str) -> None:
    """Get the compiler cache of `name` from the remote storage, when it has none, and the storage has one."""
    if not project.config.remote or local_path(project, name).exists():
        return
    try:
        fetch(project, name)
    except (remote.RemoteError, OSError) as e:
        console.print(f"[yellow]no compiler cache from the remote storage[/]: {escape(str(e))}")
