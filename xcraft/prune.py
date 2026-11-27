"""Pruning the remote storage: the builds no publication has, see docs/publishing-layout-design.md.

What is published is the reference: the builds whose packages a snapshot of the repositories of a stage has, dated or
latest, are kept, so that any published state is a starting point for a fix, as are those pushed during the grace
period, for a publication failing, its builds waiting for the next one. The latest snapshot of a cumulative stage
without its manifest, while it changes, or after a publication interrupted then, has the packages of what it was made
from. The ISOs pushed, which the snapshots of the ISOs copy, are kept the same way, by those snapshots, dated or latest:
they keep no build of their own, an ISO kept longer than the snapshots of its packages is not a starting point anymore.
The copies of Garage share their blocks: removing
the builds of a snapshot would free nothing. It must not run during a publication, which may copy a build it removes.
"""

from __future__ import annotations

import datetime as dt
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, NamedTuple

from xcraft import remote, remote_pages, upload
from xcraft.process import thread_pool
from xcraft.project import INTERNAL

if TYPE_CHECKING:
    from xcraft.project import Project, Remote

BUILDS = f"{INTERNAL}/builds"
"""The builds in the storage, a tree by signer, architecture, package and key, see xcraft.cache.build_path()."""
ISOS = f"{INTERNAL}/isos"
"""The ISOs pushed in the storage, a tree by architecture and key, see xcraft.iso.iso_path()."""
GRACE = 15
"""The days the builds and the ISOs are kept after their push, published or not."""
_KINDS = {BUILDS: ("repos", ".rpm"), ISOS: ("isos", ".iso")}
"""The publications of the trees of each prefix, their snapshots by release and stage, and the files they copy."""


class Pruned(NamedTuple):
    """What pruning removes: the trees of the builds and of the ISOs, and the files of the storage, with their total
    size.
    """

    trees: list[str]
    files: list[str]
    size: int


def _listing(storage: Remote, prefix: str) -> dict[str, remote.Entry]:
    """Return the files below `prefix`, by path in the storage, with their sizes and dates, but the pages."""
    with remote.writing(storage, f"list {prefix}") as s3:
        return {f"{prefix}{f}": entry for f, entry in remote.objects(s3, storage.bucket, prefix, below=True)[1].items()}


def _manifests(files: dict[str, remote.Entry]) -> list[str]:
    """Return the trees of `files`, those with a manifest."""
    return sorted(f.removesuffix(f"/{remote.MANIFEST}") for f in files if f.endswith(f"/{remote.MANIFEST}"))


def _packages(files: dict[str, str], suffix: str) -> set[str]:
    """Return the SHA-256 of the packages, or ISOs, of `files`, by their `suffix`: not of the other files, like the
    public key of a signer, which the builds and the snapshots it signs all have.
    """
    return {digest for f, digest in files.items() if f.endswith(suffix)}


def _inputs(storage: Remote, tree: str) -> dict[str, str]:
    """Return the files of what the latest snapshot of the tree of inputs `tree` was made from, without checking its
    signature, or none.
    """
    if not (found := remote.unverified_manifest(storage, tree)):
        return {}
    with tempfile.TemporaryDirectory(prefix="xcraft-inputs-") as tmp:
        try:
            if not remote.download(storage, f"{found[0]}/{upload.INPUTS}", path := Path(tmp) / upload.INPUTS):
                return {}
            return upload.SnapshotInputs.model_validate_json(path.read_bytes()).files
        except ValueError:
            return {}


def _published(storage: Remote, published: str, suffix: str) -> set[str]:
    """Return the SHA-256 of the packages, or ISOs, by their `suffix`, of the publications of the storage, `published`
    below, and of what their latest snapshots were made from.
    """
    trees = _manifests(_listing(storage, f"{published}/"))
    inputs = _manifests(_listing(storage, f"{upload.inputs_tree(published)}/"))
    with thread_pool(remote.REQUESTS) as pool:
        manifests = [
            found[1] for found in pool.map(lambda tree: remote.unverified_manifest(storage, tree), trees) if found
        ]
        made = list(pool.map(lambda tree: _inputs(storage, tree), inputs))
    return {digest for files in [*manifests, *made] for digest in _packages(files, suffix)}


def _ancestors(path: str) -> list[str]:
    """Return the directories `path` is in, the nearest first."""
    return [str(d) for d in PurePosixPath(path).parents][:-1]


def pruned(storage: Remote, keep_days: int, now: dt.datetime) -> Pruned:
    """Return what pruning the storage removes: the builds whose packages no publication has, and the ISOs no snapshot
    of the ISOs has, pushed more than `keep_days` ago, and the files of the generations of the others their manifests
    do not have anymore, or of no tree, written more than `keep_days` ago.

    A tree whose manifest cannot be read is pruned too: no command uses it.
    """
    since = now - dt.timedelta(days=keep_days)
    found = [_pruned(storage, prefix, *kind, since) for prefix, kind in _KINDS.items()]
    return Pruned([t for f in found for t in f.trees], [x for f in found for x in f.files], sum(f.size for f in found))


def _pruned(storage: Remote, prefix: str, published_in: str, suffix: str, since: dt.datetime) -> Pruned:
    """Return what pruning the trees below `prefix` removes, those whose files of `suffix` no publication of
    `published_in` has, and the garbage, older than `since`, see pruned().
    """
    files, published = _listing(storage, f"{prefix}/"), _published(storage, published_in, suffix)
    trees = _manifests(files)
    with thread_pool(remote.REQUESTS) as pool:
        manifests = dict(
            zip(trees, pool.map(lambda tree: remote.unverified_manifest(storage, tree), trees), strict=True)
        )
    below: dict[str | None, list[str]] = {}
    for f in files:
        below.setdefault(next((d for d in _ancestors(f) if d in manifests), None), []).append(f)
    removed, garbage = [], [f for f in below.get(None, []) if files[f][1] < since]
    for tree, found in manifests.items():
        if files[f"{tree}/{remote.MANIFEST}"][1] >= since or (found and published & _packages(found[1], suffix)):
            root, kept = found or (tree, {})
            used = {f"{root}/{f}" for f in kept} | {f"{tree}/{remote.MANIFEST}"}
            garbage += [f for f in below[tree] if f not in used and files[f][1] < since]
        else:
            removed.append(tree)
            garbage += below[tree]
    return Pruned(removed, sorted(garbage), sum(files[f][0] for f in garbage))


def prune(project: Project, storage: Remote, pruning: Pruned) -> None:
    """Remove from the storage what `pruning` has, the manifests first: an interrupted pruning must not leave a tree
    trusted without all its files; with the pages of the directories emptied, and those of their parents updated.
    """
    manifests = [f for f in pruning.files if f.endswith(f"/{remote.MANIFEST}")]
    remote.delete(storage, manifests)
    remote.delete(storage, [f for f in pruning.files if f not in manifests])
    prefixes = tuple(f"{prefix}/" for prefix in _KINDS)
    used = {d for prefix in prefixes for f in _listing(storage, prefix) for d in _ancestors(f)}
    emptied = {d for f in pruning.files for d in _ancestors(f) if d.startswith(prefixes) and d not in used}
    remote.delete(storage, [f"{d}/{remote.INDEX}" for d in sorted(emptied)])
    parents = {_ancestors(f)[0] for f in [*pruning.files, *emptied]} - emptied
    remote_pages.update_pages(project, storage, sorted(f"{d}/" for d in parents))
