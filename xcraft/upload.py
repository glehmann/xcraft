"""Publishing the snapshots made by xcraft.publish to the remote storage: uploaded, or the latest one reused.

The repositories and the ISOs of a snapshot are published as ``<kind>/<release>/<stage>/<name>/``, with ``repos`` or
``isos`` as kind, and copied as ``<kind>/<release>/<stage>/latest/``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel
from rich.markup import escape

from xcraft import buildid, remote, remote_pages
from xcraft.cache import BuildError, build_keys, build_path
from xcraft.display import activity, console
from xcraft.iso import iso_inputs
from xcraft.localrepo import OUTPUT_MARKER
from xcraft.project import INTERNAL
from xcraft.publish import ISO_DIR, REPO, Stored, snapshot_key, snapshot_tree, trusted_key
from xcraft.signing import Signer, key_signer
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from xcraft.project import Project, Remote

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


def inputs_tree(latest: str) -> str:
    """Return the tree of what the latest snapshot `latest` was made from, out of it, in the storage."""
    return f"{INTERNAL}/inputs/{latest}"


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
        manifests = {kind: self._reusable(kind, keys) for kind, keys in wanted.items()}
        if not manifests or not all(manifests.values()):
            return None
        name = self.snapshot_name()
        for i, (kind, manifest) in enumerate(manifests.items()):
            own = repo_files(name, manifest) if repo_files and kind == "repos" else {}
            if not self._copy_latest(kind, name, manifest, wanted[kind], own):  # all assembled again, as `name`
                for copied in list(manifests)[: i + 1]:
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

    def _reusable(self, kind: str, keys: dict[str, str]) -> dict[str, str]:
        """Return the manifest of the latest snapshot of `kind`, when it was made from the input `keys`: none
        otherwise.
        """
        latest = snapshot_tree(self.project, kind, "latest")
        with self.project.tmp_dir("snapshot-inputs") as work:
            try:
                if not remote.download_tree(self.storage, inputs_tree(latest), work / "inputs", trusted=self._trusted):
                    return {}
                inputs = SnapshotInputs.model_validate_json((work / "inputs" / INPUTS).read_bytes())
            except (OSError, ValueError, remote.RemoteError):
                return {}
        manifest = remote.read_manifest(self.storage, latest, trusted=self._trusted)
        return manifest if manifest and inputs.keys == keys and inputs.files == manifest else {}

    def _write_inputs(self, latest: str, keys: dict[str, str], manifest: dict[str, str]) -> None:
        """Write what the latest snapshot `latest` was made from: its input `keys`, its `manifest`, and the commit."""
        with self.project.tmp_dir("snapshot-inputs") as work:
            snapshot = SnapshotInputs(keys=keys, files=manifest, commit=buildid.commit(self.project))
            (work / INPUTS).write_text(snapshot.model_dump_json())
            remote.upload_tree(self.storage, inputs_tree(latest), work, [INPUTS], signer=self._signing)

    def _copy_latest(
        self, kind: str, name: str, manifest: dict[str, str], keys: dict[str, str], own: dict[str, str]
    ) -> bool:
        """Copy the latest snapshot of `kind`, with its `manifest`, made from the input `keys`, as `name`, with the
        commit of this publication, but its files of `own`, by path, written with their texts, in the latest snapshot
        too, which is the new one, and return whether it could.
        """
        latest, target = snapshot_tree(self.project, kind, "latest"), snapshot_tree(self.project, kind, name)
        with (
            self.project.tmp_dir("snapshot-files") as work,
            activity(f"reusing the published {kind} as {target}", log=True) as on_line,
        ):
            for f, text in own.items():
                (work / f).write_text(text)
            files = manifest | {f: sha256_of(work / f) for f in own}
            try:
                for batch in _batches(list(files)):
                    sources = {f"{target}/{f}": work / f if f in own else f"{latest}/{f}" for f in batch}
                    remote.transfer(self.storage, sources, on_line)
            except remote.RemoteError as e:  # a file missing in the latest snapshot
                console.print(f"[yellow]the published {kind} cannot be reused[/]: {escape(str(e))}")
                return False
            remote.write_manifest(self.storage, target, files, signer=self._signing)
            _delete_stale(self.storage, target, {*files, remote.MANIFEST})
            if files != manifest:  # without its manifest while it changes, see _upload()
                remote.delete(self.storage, [f"{latest}/{remote.MANIFEST}"])
                remote.transfer(self.storage, {f"{latest}/{f}": work / f for f in own}, on_line)
                remote.write_manifest(self.storage, latest, files, signer=self._signing)
            self._write_inputs(latest, keys, files)
            remote_pages.update_pages(self.project, self.storage, [f"{target}/", f"{latest}/"])
        console.print(f"[bold green]✓[/] published {target}, and as {latest} (reused the matching snapshot)")
        return True

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
        """Upload the ISOs of the snapshot in `src`, in ISO_DIR, of all the architectures, as `name`, with their
        checksums, in SHA256SUMS, signed by `signer`, if any, and make them the latest ones.
        """
        isos = src / ISO_DIR
        sums = isos / SUMS
        sums.write_text("".join(f"{sha256_of(iso)}  {iso.name}\n" for iso in sorted(isos.glob("*.iso"))))
        if self.signer:
            self.signer.sign_file(sums)
        made = [iso.name for iso in isos.glob("*.iso")]
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
        manifest = _manifest(src, stored)
        previous = remote.read_manifest(self.storage, latest, trusted=self._trusted)
        candidates = {f: Stored(f"{latest}/{f}", digest) for f, digest in previous.items()} | stored
        copies = {f: s.path for f, s in candidates.items() if manifest.get(f) == s.digest}
        with activity(f"publishing {tree}", log=True) as on_line:
            self._write(tree, manifest, {f: copies.get(f, src / f) for f in manifest}, on_line)
            # only the files changed: without its manifest, a latest tree left incomplete is copied entirely next time
            remote.delete(self.storage, [f"{latest}/{remote.MANIFEST}"])
            changed = {f: f"{tree}/{f}" for f in manifest if previous.get(f) != manifest[f]}
            self._write(latest, manifest, changed, on_line)
            self._write_inputs(latest, inputs, manifest)
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
    ) -> None:
        """Write the `files` to `tree`, from their sources, by batches, then its `manifest`, deleting the files
        of a failed publish of the same name, and of the previous latest snapshot, but the pages.
        """
        for batch in _batches(sorted(files)):
            remote.transfer(self.storage, {f"{tree}/{f}": files[f] for f in batch}, on_line)
        remote.write_manifest(self.storage, tree, manifest, signer=self._signing)
        _delete_stale(self.storage, tree, {*manifest, remote.MANIFEST})
