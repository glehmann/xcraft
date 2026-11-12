"""Our copies of the pinned upstream packages signed again, once by key, for the snapshots of final mode, which the
hosts install with our keys only: see docs/publishing-layout-design.md.

The originals stay as they are, in the upstream copy, see xcraft.upstream_copy. A copy signed by a key is
``<sha256 of the original>/<file>``: found by the SHA-256 the lock pins, whatever the signature, which has a date. It
is in the cache, ``.xcraft/cache/upstream-signed/<fingerprint>/``, and in the remote storage, in a repository by key
and architecture, ``.xcraft/upstream-signed/<fingerprint>/<arch>/``, append-only and kept without expiry like the
originals, whose metadata is updated like theirs, without downloading its packages.
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from xcraft import remote_pages, repodata, upstream_copy
from xcraft.display import activity
from xcraft.project import INTERNAL
from xcraft.signing import check_signatures
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project, Remote
    from xcraft.repodata import Entry
    from xcraft.signing import Signer

PREFIX = f"{INTERNAL}/upstream-signed"


def _prefix(signer: Signer) -> str:
    return f"{PREFIX}/{signer.key}"


def path(project: Project, signer: Signer, location: str) -> str:
    """Return the path in the storage of the copy signed by `signer` at `location`, ``<sha256>/<file>``."""
    return f"{_prefix(signer)}/{project.arch}/{location}"


def read(project: Project, storage: Remote, signer: Signer, work: Path) -> tuple[dict[str, Entry], list[str]]:
    """Return the metadata entries of the copies of the storage signed by `signer`, for the architecture of `project`,
    by SHA-256, read in `work`, and the paths of their metadata files: their metadata is signed by `signer` too, as
    the bucket is not, see upstream_copy.read().
    """
    return upstream_copy.read(storage, project.arch, work, _prefix(signer), signer)


def by_original(entries: dict[str, Entry]) -> dict[str, tuple[Entry, str]]:
    """Return the copies of `entries`, by the SHA-256 of their originals, with their own."""
    return {repodata.location(entry).split("/")[0]: (entry, digest) for digest, entry in entries.items()}


def signed(project: Project, signer: Signer, rpms: dict[str, Path]) -> dict[str, Path]:
    """Return the copies of the original RPMs `rpms`, by SHA-256, signed by `signer`: from the cache, or signed now,
    checked, and cached, once by key.
    """
    cache = project.state / "cache/upstream-signed" / signer.key
    if todo := {d: rpm for d, rpm in rpms.items() if not (cache / d / rpm.name).exists()}:
        with project.tmp_dir("upstream-signed") as work:
            for digest, rpm in todo.items():
                (work / digest).mkdir()
                shutil.copy2(rpm, work / digest / rpm.name)  # a copy: signing changes it
            signer.sign_rpms(project, work)
            files = [f"{d}/{rpm.name}" for d, rpm in todo.items()]
            check_signatures(
                project, work, files, what="upstream packages signed again", public_key=signer.public_key()
            )
            for f in files:
                (cache / f).parent.mkdir(parents=True, exist_ok=True)
                (work / f).rename(cache / f)
    return {d: cache / d / rpm.name for d, rpm in rpms.items()}


def push(
    project: Project,
    storage: Remote,
    signer: Signer,
    copies: dict[str, Path],
    read_in: Path,
    known: tuple[dict[str, Entry], list[str]],
) -> dict[str, tuple[Entry, str]]:
    """Add the `copies` signed by `signer`, by the SHA-256 of their originals, to those of the storage, `known`, read in
    `read_in`, and return their metadata entries, by the SHA-256 of their originals, with their own.
    """
    entries, hrefs = known
    files: upstream_copy.Copies = {f"{d}/{rpm.name}": (rpm, None) for d, rpm in copies.items()}
    new = upstream_copy.update(project, storage, project.arch, read_in, entries, hrefs, files, _prefix(signer), signer)
    with activity("making the pages of the directories"):
        remote_pages.update_pages(project, storage, [f"{_prefix(signer)}/"])
    return {f.split("/")[0]: (entry, sha256_of(copies[f.split("/")[0]])) for f, entry in new.items()}
