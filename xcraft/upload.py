"""Publishing the snapshots made by xcraft.publish to the remote storage: uploaded, or the latest one reused.

The repositories and the ISOs of a snapshot are published as ``<kind>/<release>/<stage>/<name>/``, with ``repos`` or
``isos`` as kind, and copied as ``<kind>/<release>/<stage>/latest/``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel
from rich.markup import escape

from xcraft import buildid, metalink, remote, remote_pages, repodata
from xcraft.cache import BuildError, build_keys, build_path
from xcraft.display import activity, console
from xcraft.iso import iso_inputs
from xcraft.localrepo import OUTPUT_MARKER
from xcraft.project import INTERNAL
from xcraft.publish import ISO_DIR, REPO, Stored, published_packages, snapshot_key, snapshot_tree, trusted_key
from xcraft.signing import Signer, key_signer
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from xcraft.project import Project, Remote, Retention
    from xcraft.repodata import Entry

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
    commit: str | None = None
    """The git commit of its publication, a maintenance branch of the release starts from, see xcraft maint start:
    <commit>-dirty for a checkout with changes, see xcraft.buildid.commit(), none for the snapshots published before it
    was recorded."""
    snapshot: str | None = None
    """The name of the dated snapshot of a cumulative stage, whose latest one has the older versions too."""
    left: dict[str, str] = {}
    """When each older version of the latest snapshot of a cumulative stage left it, by path."""


def inputs_tree(latest: str) -> str:
    """Return the tree of what the latest snapshot `latest` was made from, out of it, in the storage."""
    return f"{INTERNAL}/inputs/{latest}"


def _delete_stale(storage: Remote, prefix: str, keep: set[str]) -> None:
    """Delete the files under `prefix` but those of `keep`, and the pages of the directories, written again after."""
    remote.delete(storage, [f"{prefix}/{p}" for p in remote.listing(storage, f"{prefix}/") if p not in keep])


def remove_trees(project: Project, storage: Remote, prefixes: list[str]) -> int:
    """Remove the directories `prefixes` from the storage, with the pages of their directories, and update those of
    their parents, and return the number of files removed.

    The manifests go first: an interrupted removal must not leave a tree trusted without all its files.
    """
    files = [f"{prefix}/{f}" for prefix in prefixes for f in remote.listing(storage, f"{prefix}/")]
    directories = {
        str(d) for f in files for d in PurePosixPath(f).parents if any(f"{d}/".startswith(f"{p}/") for p in prefixes)
    }
    manifests = [f for f in files if f.endswith(f"/{remote.MANIFEST}")]
    remote.delete(storage, manifests)
    remote.delete(
        storage, [f for f in files if f not in manifests] + [f"{d}/{remote.INDEX}" for d in sorted(directories)]
    )
    parents = sorted({f"{PurePosixPath(prefix).parent}/" for prefix in prefixes})
    remote_pages.update_pages(project, storage, parents)
    return len(files)


_DATED = re.compile(r"(\d{4}-\d{2}-\d{2})(\.\d+)?")
"""The name of a dated snapshot, by the day of its publication, with a suffix after the first one of the day."""


def expired_snapshots(names: set[str], days: int, today: date) -> list[str]:
    """Return those of the snapshots `names` published more than `days` ago, but the latest one, which is kept."""
    dated = {name: m for name in names if (m := _DATED.fullmatch(name))}
    newest = max(dated, key=lambda n: (dated[n][1], int(dated[n][2][1:] if dated[n][2] else 1)), default=None)
    return sorted(n for n, m in dated.items() if n != newest and (today - date.fromisoformat(m[1])).days > days)


def _source(path: str) -> bool:
    """Return whether the package `path` of a snapshot is a source package."""
    return path.split("/")[1] == "Source"


def _name_arch(entry: Entry) -> tuple[str, str]:
    """Return the name and the architecture of a package, from its metadata."""
    name, _, arch = repodata.nevra(entry)
    return name, arch


def _repos(manifest: dict[str, str]) -> list[str]:
    """Return the repositories of a snapshot of `manifest`, by path in it."""
    return sorted(f.removesuffix("/repodata/repomd.xml") for f in manifest if f.endswith("/repodata/repomd.xml"))


def _metadata(manifest: dict[str, str]) -> set[str]:
    """Return the metadata files of the repositories of a snapshot of `manifest`."""
    return {f for f in manifest if "/repodata/" in f}


def kept_versions(
    older: dict[str, tuple[str, str]],
    left: dict[str, datetime],
    current: set[tuple[str, str, str]],
    retention: Retention,
    now: datetime,
) -> set[str]:
    """Return those of the `older` binary packages, by path, with their names and architectures, that the latest
    snapshot of a cumulative stage keeps, with the `retention` of the stage: those which `left` it `days` ago at most,
    and the newest ones of the packages it has, `current` by repository, name and architecture, `keep` with the
    current version, however old: those which left it last, a publication replacing one version of a package at most.
    """
    by_package: dict[tuple[str, str, str], list[str]] = {}
    for path, (name, arch) in older.items():
        by_package.setdefault((path.rsplit("/", 1)[0], name, arch), []).append(path)
    kept = set()
    for package, paths in by_package.items():
        newest = sorted(paths, key=lambda p: (left[p], p), reverse=True)
        protected = newest[: retention.keep - 1] if package in current else []
        kept |= {p for p in paths if p in protected or retention.days is None or (now - left[p]).days <= retention.days}
    return kept


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


def _manifest(src: Path, stored: dict[str, Stored]) -> dict[str, str]:
    """Return the SHA-256 of the files of `src`, but the ISOs of a snapshot, and of those of `stored` not there, by
    path.
    """
    files = {
        f.relative_to(src).as_posix(): f
        for f in sorted(src.rglob("*"))
        if f.is_file() and f.name != OUTPUT_MARKER and not f.is_relative_to(src / ISO_DIR)
    }
    return {f: sha256_of(path) for f, path in files.items()} | {f: d for f, (_, d) in stored.items() if f not in files}


@dataclass
class Uploader:
    """Publishing the snapshots of the stage of `project` to `storage`, signed by `signer`, if any, with the build
    repository with `build`.
    """

    project: Project
    storage: Remote
    signer: Signer | None
    build: bool
    dev: bool = False
    """Whether the snapshots are of dev mode, whose packages keep the signatures of their builds."""

    @property
    def _signing(self) -> Signer | None:
        """The key signing the trees: the one of the snapshot, or that of xcraft.toml."""
        return self.signer or key_signer(self.project)

    @property
    def _trusted(self) -> Signer | None:
        return trusted_key(self.project, self.signer)

    def snapshot_name(self) -> str:
        """Return the name of a new snapshot: today's date, with the first suffix used neither by repositories nor
        ISOs.
        """
        day = datetime.now(UTC).date().isoformat()
        for name in (day, *(f"{day}.{n}" for n in range(2, 1000))):
            if not (
                remote.exists(
                    self.storage, f"{snapshot_tree(self.project, 'repos', name)}/{REPO}/Source/repodata/repomd.xml"
                )
                or remote.exists(self.storage, f"{snapshot_tree(self.project, 'isos', name)}/{remote.MANIFEST}")
            ):
                return name
        msg = f"too many snapshots on {day}"
        raise BuildError(msg)

    def expire(self) -> None:
        """Remove the dated snapshots of the stage, of its repositories and of its ISOs, published more than its
        snapshot_days ago, but the latest one of each.
        """
        if not (days := self.project.config.publish.current().snapshot_days):
            return
        today = datetime.now(UTC).date()
        trees = [
            snapshot_tree(self.project, kind, name)
            for kind in ("repos", "isos")
            for name in expired_snapshots(
                remote.subdirectories(self.storage, snapshot_tree(self.project, kind, "")), days, today
            )
        ]
        if trees:
            with activity(f"removing the {len(trees)} snapshots published more than {days} days ago", log=True):
                remove_trees(self.project, self.storage, trees)
            console.print(f"removed {', '.join(trees)}, published more than {days} days ago")

    def reuse(
        self,
        platforms: list[Project],
        *,
        only: str | None,
        isos: list[Project],
        repo_files: Callable[[str, dict[str, str]], dict[str, str]] | None = None,
    ) -> str | None:
        """Publish the latest snapshot of the `platforms` again, as a new one, by copies in the storage, when it is
        trusted and made of the same, and return its name, if it was: its repositories with their own files of
        `repo_files`, by path, given the name and the manifest, like the .repo files, rather than those of the latest
        one.
        """
        wanted = self._inputs(platforms, only, isos)
        sources = {kind: found for kind, keys in wanted.items() if (found := self._reusable(kind, keys))}
        if not wanted or len(sources) != len(wanted):
            return None
        name = self.snapshot_name()
        for i, (kind, (source, manifest)) in enumerate(sources.items()):
            own = repo_files(name, manifest) if repo_files and kind == "repos" else {}
            if not self._copy(kind, source, name, manifest, wanted[kind], own):  # all assembled again, as `name`
                for copied in list(sources)[: i + 1]:
                    _delete_stale(self.storage, snapshot_tree(self.project, copied, name), set())
                return None
        return name

    def _inputs(self, platforms: list[Project], only: str | None, isos: list[Project]) -> dict[str, dict[str, str]]:
        """Return the input keys of the snapshot to publish, by kind: of the repositories of the `platforms`, but with
        `only` isos, and of the ISOs of `isos`.
        """
        wanted = {}
        if only != "isos":
            wanted["repos"] = {
                p.arch: snapshot_key(p, build_keys(p), signer=self.signer, build=self.build, dev=self.dev)
                for p in platforms
            }
        if isos:
            wanted["isos"] = {name: key for p in isos for name, key in iso_inputs(p, signer=self.signer).items()}
        return wanted

    def _read_inputs(self, latest: str) -> SnapshotInputs | None:
        """Return what the latest snapshot `latest` was made from, if it is trusted."""
        with self.project.tmp_dir("snapshot-inputs") as work:
            try:
                if not remote.download_tree(self.storage, inputs_tree(latest), work / "inputs", trusted=self._trusted):
                    return None
                return SnapshotInputs.model_validate_json((work / "inputs" / INPUTS).read_bytes())
            except (OSError, ValueError, remote.RemoteError):
                return None

    def _reusable(self, kind: str, keys: dict[str, str]) -> tuple[str, dict[str, str]] | None:
        """Return the latest snapshot of `kind`, with its manifest, when it was made from the input `keys`: the latest
        tree, or the dated snapshot of a cumulative stage, whose latest tree has older versions too.
        """
        latest = snapshot_tree(self.project, kind, "latest")
        if not (inputs := self._read_inputs(latest)):
            return None
        manifest = remote.read_manifest(self.storage, latest, trusted=self._trusted)
        if not manifest or inputs.keys != keys or inputs.files != manifest:
            return None
        if not inputs.snapshot:
            return latest, manifest
        tree = snapshot_tree(self.project, kind, inputs.snapshot)
        return (tree, dated) if (dated := remote.read_manifest(self.storage, tree, trusted=self._trusted)) else None

    def _write_inputs(
        self,
        latest: str,
        keys: dict[str, str],
        manifest: dict[str, str],
        snapshot: str | None = None,
        left: dict[str, str] | None = None,
    ) -> None:
        """Write what the latest snapshot `latest` was made from: its input `keys`, its `manifest`, and the commit;
        of a cumulative stage, its dated `snapshot`, and the day each older version `left` it.
        """
        with self.project.tmp_dir("snapshot-inputs") as work:
            inputs = SnapshotInputs(
                keys=keys, files=manifest, commit=buildid.commit(self.project), snapshot=snapshot, left=left or {}
            )
            (work / INPUTS).write_text(inputs.model_dump_json())
            remote.upload_tree(self.storage, inputs_tree(latest), work, [INPUTS], signer=self._signing)

    def _retention(self, kind: str) -> Retention | None:
        """Return the retention of the stage, for the repositories of a cumulative one: none otherwise."""
        return self.project.config.publish.current().retention if kind == "repos" else None

    def _copy(
        self, kind: str, source: str, name: str, manifest: dict[str, str], keys: dict[str, str], own: dict[str, str]
    ) -> bool:
        """Copy the snapshot `source` of `kind`, with its `manifest`, made from the input `keys`, as `name`, with the
        commit of this publication, but its files of `own`, by path, written with their texts, in the latest snapshot
        too, which is the new one, and return whether it could.
        """
        latest, target = snapshot_tree(self.project, kind, "latest"), snapshot_tree(self.project, kind, name)
        with (
            self.project.tmp_dir("snapshot-files") as work,
            activity(f"reusing the published {kind} as {target}", log=True) as on_line,
        ):
            try:
                if kind == "repos":
                    own = own | self._metalinks(target, self._repomds(source, manifest), {})
            except BuildError as e:  # a file missing in the latest snapshot
                console.print(f"[yellow]the published {kind} cannot be reused[/]: {escape(str(e))}")
                return False
            for f, text in own.items():
                (work / f).parent.mkdir(parents=True, exist_ok=True)
                (work / f).write_text(text)
            files = manifest | {f: sha256_of(work / f) for f in own}
            try:
                for batch in _batches(list(files)):
                    sources = {f"{target}/{f}": work / f if f in own else f"{source}/{f}" for f in batch}
                    remote.transfer(self.storage, sources, on_line)
            except remote.RemoteError as e:  # a file missing in the latest snapshot
                console.print(f"[yellow]the published {kind} cannot be reused[/]: {escape(str(e))}")
                return False
            remote.write_manifest(self.storage, target, files, signer=self._signing)
            _delete_stale(self.storage, target, {*files, remote.MANIFEST})
            if retention := self._retention(kind):
                self._update_cumulative(target, name, files, keys, retention, on_line)
            else:
                self._update_latest(kind, target, files, keys, on_line)
            remote_pages.update_pages(self.project, self.storage, [f"{target}/", f"{latest}/"])
        console.print(f"[bold green]✓[/] published {target}, and as {latest} (reused the matching snapshot)")
        return True

    def _repomds(self, tree: str, manifest: dict[str, str]) -> dict[str, bytes]:
        """Return the repomd.xml of the repositories of the snapshot `tree`, with its `manifest`, by repository."""
        repomds = {}
        with self.project.tmp_dir("repomd") as work:
            for repo in _repos(manifest):
                path = f"{repo}/repodata/repomd.xml"
                if not remote.download(self.storage, f"{tree}/{path}", work / path) or (
                    sha256_of(work / path) != manifest[path]
                ):
                    msg = f"{tree}/{path} is missing or corrupted in the storage"
                    raise BuildError(msg)
                repomds[repo] = (work / path).read_bytes()
        return repomds

    def _metalinks(self, tree: str, repomds: dict[str, bytes], previous: dict[str, str]) -> dict[str, str]:
        """Return the metalinks of the repositories of the snapshot `tree`, whose repomd.xml are `repomds`, by
        repository, by path in it: on the mirrors of the stage first, for its latest snapshot, then on our server; with
        the repomd.xml of the latest snapshot of `previous` manifest as alternates.
        """
        if self.storage.private:  # which the hosts do not read
            return {}
        latest = tree.endswith("/latest")
        servers = [*(self.project.config.publish.current().mirrors if latest else []), self.storage.url]
        links = {}
        with self.project.tmp_dir("metalinks") as work:
            for repo, repomd in repomds.items():
                path = f"{repo}/repodata/{metalink.FILE}"
                before = None
                if path in previous and remote.download(self.storage, f"{tree}/{path}", work / path):
                    before = (work / path).read_text() if sha256_of(work / path) == previous[path] else None
                urls = [f"{server.rstrip('/')}/{tree}/{repo}/repodata/repomd.xml" for server in servers]
                links[path] = metalink.text(urls, repomd, before)
        return links

    def _update_latest(
        self, kind: str, tree: str, manifest: dict[str, str], keys: dict[str, str], on_line: Callable[[str], None]
    ) -> None:
        """Make the latest snapshot of `kind` of an exact stage the snapshot `tree`, with its `manifest`, made from the
        input `keys`, with metalinks of its own, writing only the files changed.

        It is without its manifest while it changes: a latest snapshot left incomplete is copied entirely next time.
        """
        latest = snapshot_tree(self.project, kind, "latest")
        previous = remote.read_manifest(self.storage, latest, trusted=self._trusted)
        with self.project.tmp_dir("latest") as work:
            links = self._metalinks(latest, self._repomds(tree, manifest), previous) if kind == "repos" else {}
            for path, text in links.items():
                (work / path).parent.mkdir(parents=True, exist_ok=True)
                (work / path).write_text(text)
            files = manifest | {path: sha256_of(work / path) for path in links}
            changed = {f: work / f if f in links else f"{tree}/{f}" for f in files if previous.get(f) != files[f]}
            if files != previous:
                remote.delete(self.storage, [f"{latest}/{remote.MANIFEST}"])
                self._write(latest, files, changed, on_line, lingering=_metadata(previous))
        self._write_inputs(latest, keys, files)

    def _update_cumulative(
        self,
        tree: str,
        name: str,
        manifest: dict[str, str],
        keys: dict[str, str],
        retention: Retention,
        on_line: Callable[[str], None],
    ) -> None:
        """Make the latest snapshot of the cumulative stage the snapshot `tree`, `name`, with its `manifest`, made
        from the input `keys`, and the older versions of the previous latest one that its `retention` keeps, with the
        metadata of all of them.

        A package of the snapshot replaces an older one of the same path, with other contents.
        """
        latest, now = snapshot_tree(self.project, "repos", "latest"), datetime.now(UTC).replace(microsecond=0)
        previous, history, complete = self._previous(latest)
        known = published_packages(self.project, self.storage, self.signer, latest, previous)[0] if previous else {}
        new = published_packages(self.project, self.storage, self.signer, tree)[0]
        entries = {f: new[d][0] for f, d in manifest.items() if f.endswith(".rpm") and d in new}
        if replaced := sorted(f for f, d in previous.items() if f in manifest and d != manifest[f] and f in entries):
            console.print(f"[yellow]replaced by other contents of the same name[/]: {escape(', '.join(replaced))}")
        olds = {f: d for f, d in previous.items() if f.endswith(".rpm") and f not in manifest}
        if unread := sorted(f for f, d in olds.items() if d not in known):
            msg = f"the metadata of {', '.join(unread)} cannot be read in {latest}, whose older versions would be lost"
            raise BuildError(msg)
        older = {f: known[d][0] for f, d in olds.items()}
        left = {f: datetime.fromisoformat(history[f]) if f in history else now for f in older}
        current = {(f.rsplit("/", 1)[0], *_name_arch(e)) for f, e in entries.items() if not _source(f)}
        binaries = {f: _name_arch(e) for f, e in older.items() if not _source(f)}
        kept = kept_versions(binaries, left, current, retention, now)
        srpms = {(f.split("/")[0], repodata.sourcerpm(older[f])) for f in kept}
        kept |= {f for f in older if _source(f) and (f.split("/")[0], f.rsplit("/", 1)[1]) in srpms}
        files = {f: d for f, d in manifest.items() if "/repodata/" not in f} | {f: previous[f] for f in kept}
        sources: dict[str, str | Path] = {f: f"{tree}/{f}" for f in manifest}
        repos = _repos(manifest)
        with self.project.tmp_dir("cumulative") as work:
            listed, unchanged = entries | {f: older[f] for f in kept}, []
            for repo in repos:
                packages = {f: d for f, d in files.items() if f.rsplit("/", 1)[0] == repo}
                if complete and packages == {f: d for f, d in previous.items() if f.rsplit("/", 1)[0] == repo}:
                    # the same metadata, rather than a new repomd.xml, which the hosts would download for nothing
                    files |= {f: d for f, d in previous.items() if f.startswith(f"{repo}/repodata/")}
                    unchanged.append(f"{repo}/repodata/repomd.xml")
                    continue
                repodata.write(work / repo, {f.rsplit("/", 1)[1]: listed[f] for f in packages if f in listed})
                if self.signer:
                    self.signer.sign_file(work / repo / "repodata/repomd.xml")
            repomds = {
                repo: (work / repo / "repodata/repomd.xml").read_bytes()
                for repo in repos
                if f"{repo}/repodata/repomd.xml" not in unchanged
            } | self._repomds(latest, {f: previous[f] for f in unchanged})
            for path, text in self._metalinks(latest, repomds, previous).items():
                (work / path).parent.mkdir(parents=True, exist_ok=True)
                (work / path).write_text(text)
            for repo in repos:
                for path in (work / repo / "repodata").glob("*"):
                    files[f"{repo}/repodata/{path.name}"] = sha256_of(path)
                    sources[f"{repo}/repodata/{path.name}"] = path
            # without its manifest while it changes, see _upload()
            remote.delete(self.storage, [f"{latest}/{remote.MANIFEST}"])
            # all of them, after a publication interrupted, which may have written some already
            changed = {f: sources[f] for f in files if previous.get(f) != files[f] or (f in sources and not complete)}
            self._write(latest, files, changed, on_line, lingering=_metadata(previous))
        self._write_inputs(latest, keys, files, name, {f: left[f].isoformat() for f in sorted(kept)})

    def _previous(self, latest: str) -> tuple[dict[str, str], dict[str, str], bool]:
        """Return the manifest of the latest snapshot `latest` of a cumulative stage, when its older versions left it,
        and whether it is complete: otherwise, the manifest of what it was made from, without its own while it
        changes, or after a publication interrupted then.

        It raises when it, or what it was made from, has a manifest no key trusted signs: its older versions would be
        lost.
        """
        inputs = self._read_inputs(latest)
        if manifest := remote.read_manifest(self.storage, latest, trusted=self._trusted):
            return manifest, inputs.left if inputs else {}, True
        if inputs:
            return inputs.files, inputs.left, False
        for tree in (latest, inputs_tree(latest)):
            if remote.exists(self.storage, f"{tree}/{remote.MANIFEST}"):
                msg = f"{tree} has a manifest no trusted key signs: the older versions of {latest} would be lost"
                raise BuildError(msg)
        return {}, {}, True

    def upload_snapshot(
        self,
        platforms: list[tuple[Project, dict[str, str]]],
        src: Path,
        name: str,
        *,
        stored: dict[str, Stored],
    ) -> None:
        """Upload the repositories of the snapshot in `src` of the `platforms`, with the keys of their packages, as
        `name`, and make them the latest ones.

        The packages already in the storage, in the latest snapshot or the builds with the keys of the platforms, are
        copied there, and so are those of `stored`, which are not in `src`, by path in the snapshot, with their paths in
        the storage and SHA-256.
        """
        signed = [
            f"{INTERNAL}/builds/{self.signer.key}/{p.arch}/" for p, _ in platforms if not self.dev and self.signer
        ]
        self._upload(
            src,
            "repos",
            name,
            inputs={
                p.arch: snapshot_key(p, keys, signer=self.signer, build=self.build, dev=self.dev)
                for p, keys in platforms
            },
            stored=self._built(platforms) | stored,
            pages=signed,
        )

    def _built(self, platforms: list[tuple[Project, dict[str, str]]]) -> dict[str, Stored]:
        """Return the packages of the trees of the builds of the `platforms` in the storage, by path in the snapshot,
        with their paths there and SHA-256: those of the first architecture, for the source packages.
        """
        stored: dict[str, Stored] = {}
        if not (signer := self.signer) or self.dev:  # of other keys, in the snapshot's stored ones then
            return stored
        for platform, keys in platforms:
            repos = {"RPMS": f"{REPO}/{platform.arch}", "SRPMS": f"{REPO}/Source"}
            for package, key in keys.items():
                tree = build_path(platform, package, key, signer.key)
                root, found = remote.read_tree(self.storage, tree, trusted=self._trusted, signer=signer.key)
                stored = {
                    f"{repo}/{Path(path).name}": Stored(f"{root}/{path}", digest)
                    for path, digest in found.items()
                    if (repo := repos.get(path.split("/")[0]))
                } | stored
        return stored

    def upload_isos(self, src: Path, name: str, *, stored: dict[str, Stored] | None = None) -> None:
        """Upload the ISOs of the snapshot in `src`, in ISO_DIR, of all the architectures, as `name`, and those of
        `stored`, copied in the storage, with their checksums, in SHA256SUMS, signed by `signer`, if any, and make them
        the latest ones.
        """
        isos = src / ISO_DIR
        isos.mkdir(parents=True, exist_ok=True)
        digests = {f: s.digest for f, s in (stored or {}).items() if f.endswith(".iso")}
        digests |= {iso.name: sha256_of(iso) for iso in isos.glob("*.iso")}
        sums = isos / SUMS
        sums.write_text("".join(f"{digest}  {iso}\n" for iso, digest in sorted(digests.items())))
        if self.signer:
            self.signer.sign_file(sums)
        made = list(digests)
        inputs = {
            name: key
            for p in self.project.all_platforms()
            if any(iso.endswith((f"-{p.arch}.iso", f"-{p.arch}-netinstall.iso")) for iso in made)
            for name, key in iso_inputs(p, signer=self.signer).items()
        }
        self._upload(isos, "isos", name, inputs=inputs, stored=stored or {})

    def _upload(
        self,
        src: Path,
        kind: str,
        name: str,
        *,
        inputs: dict[str, str],
        stored: dict[str, Stored],
        pages: list[str] | None = None,
    ) -> None:
        """Upload the files of `src` as the snapshot `name` of `kind`, and as the latest one too, made from the input
        keys `inputs`, and write the pages of their directories and of the directories `pages`.

        The files the storage has already, in the latest snapshot, or in `stored`, as path and SHA-256 by file, are
        copied there, with those of `stored` not in `src`.
        """
        tree, latest = snapshot_tree(self.project, kind, name), snapshot_tree(self.project, kind, "latest")
        if kind == "repos":
            repomds = {
                repo.parent.parent.relative_to(src).as_posix(): repo.read_bytes()
                for repo in src.glob("*/*/repodata/repomd.xml")
            }
            for path, text in self._metalinks(tree, repomds, {}).items():
                (src / path).write_text(text)
        manifest = _manifest(src, stored)
        previous = remote.read_manifest(self.storage, latest, trusted=self._trusted)
        candidates = {f: Stored(f"{latest}/{f}", digest) for f, digest in previous.items()} | stored
        copies = {f: s.path for f, s in candidates.items() if manifest.get(f) == s.digest}
        with activity(f"publishing {tree}", log=True) as on_line:
            self._write(tree, manifest, {f: copies.get(f, src / f) for f in manifest}, on_line)
            if retention := self._retention(kind):
                self._update_cumulative(tree, name, manifest, inputs, retention, on_line)
            else:
                self._update_latest(kind, tree, manifest, inputs, on_line)
            on_line("the pages of the directories")
            pages = [f"{tree}/", f"{latest}/", f"{inputs_tree(latest)}/", *(pages or [])]
            remote_pages.update_pages(self.project, self.storage, pages)
        console.print(f"[bold green]✓[/] published {tree}, and as {latest}")

    def _write(
        self,
        tree: str,
        manifest: dict[str, str],
        files: Mapping[str, str | Path | remote.Located],
        on_line: Callable[[str], None],
        lingering: set[str] | None = None,
    ) -> None:
        """Write the `files` to `tree`, from their sources, by batches, then its `manifest`, deleting the files
        of a failed publish of the same name, and of the previous latest snapshot, but the pages, and the `lingering`
        ones: the metadata of the previous latest snapshot, which a host may read until the next publication.
        """
        for batch in _batches(sorted(files)):
            remote.transfer(self.storage, {f"{tree}/{f}": files[f] for f in batch}, on_line)
        remote.write_manifest(self.storage, tree, manifest, signer=self._signing)
        _delete_stale(self.storage, tree, {*manifest, *(lingering or ()), remote.MANIFEST})
