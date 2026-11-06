"""Source archives: the ``sources`` manifest of a package, the local source cache, and their downloads.

Archives are not committed with the specs. A package's ``sources`` file lists
them as ``<sha256>  <filename>`` lines, and the cache stores them as
``<cache_dir>/<sha256>/<filename>``. Missing ones come from the remote storage,
or else from the URLs of the spec.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, Field, ValidationError, field_validator

from xcraft import process
from xcraft.display import activity, download_progress
from xcraft.project import INTERNAL, write_atomically

if TYPE_CHECKING:
    from xcraft.project import Project


class SourceError(RuntimeError):
    """Raised when an archive cannot be downloaded, or does not match its manifest."""


class SourceManifest(BaseModel):
    """The archives of a ``sources`` manifest, by file name, with their SHA-256: names of files of the source cache."""

    files: dict[
        Annotated[str, Field(pattern=r"^[^/\s](?:[^/]*[^/\s])?$")], Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    ]

    @field_validator("files")
    @classmethod
    def _names(cls, files: dict[str, str]) -> dict[str, str]:
        if bad := [name for name in files if name in (".", "..") or not name.isprintable()]:
            msg = f"invalid file name: {bad[0]!r}"
            raise ValueError(msg)
        return files


def sha256_of(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def parse_manifest(text: str) -> dict[str, str]:
    """Return the filename to SHA-256 mapping of the content of a ``sources`` manifest, and raise SourceError for
    the lines that are not a SHA-256 and a file name, or name a file twice.
    """
    lines = [[*line.split(maxsplit=1), ""][:2] for line in text.splitlines() if line]
    if len(files := {name.rstrip(): digest for digest, name in lines}) != len(lines):
        msg = "a file is twice in the sources manifest"
        raise SourceError(msg)
    try:
        return SourceManifest(files=files).files
    except ValidationError as e:
        msg = f"invalid sources manifest: {e.errors()[0]['msg']}: {e.errors()[0].get('input')!r}"
        raise SourceError(msg) from None


def read_manifest(path: Path) -> dict[str, str]:
    """Return the filename to SHA-256 mapping of a ``sources`` manifest."""
    return parse_manifest(path.read_text())


def write_manifest(path: Path, entries: dict[str, str]) -> None:
    """Write a ``sources`` manifest from a filename to SHA-256 mapping."""
    write_atomically(path, "".join(f"{digest}  {name}\n" for name, digest in sorted(entries.items())))


def cache_source(cache_dir: Path, path: Path) -> str:
    """Store the archive at `path` in the source cache, and return its SHA-256."""
    digest = sha256_of(path)
    dest = cache_dir / digest / path.name
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    return digest


def archive_name(url: str) -> str:
    """Return the file name of the archive at `url`: after its ``#/``, like rpm, or the last part of its path."""
    return url.rsplit("#/", maxsplit=1)[-1] if "#/" in url else urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]


def source_path(filename: str, digest: str) -> str:
    """Return the path of a source archive in the remote storage."""
    return f"{INTERNAL}/sources/{digest}/{filename}"


def _fetch_source(project: Project, name: str, filename: str, digest: str) -> bool:
    """Download an archive of `name` into the source cache, and return whether it was found.

    It comes from the remote storage, or else from the source of the spec of `name` whose URL is its own.
    """
    from xcraft import remote  # noqa: PLC0415, which uses sha256_of()

    archive = project.source_cache / digest / filename
    archive.parent.mkdir(parents=True, exist_ok=True)
    # one of its own, next to it, for the commands downloading it at once
    fd, path = tempfile.mkstemp(dir=archive.parent, prefix=f".{filename}.", suffix=".part")
    os.close(fd)  # written by path
    part = Path(path)
    urls = (s for s in project.parsed[name].sources if "://" in s and archive_name(s) == filename)
    url, error = next(urls, ""), None

    with activity(f"downloading {filename}") as on_line:
        # from where it may be, until it matches
        for origin in [*remote.storages(project), *([url] if url else [])]:
            try:
                if isinstance(origin, str):
                    process.download(origin, part, download_progress(on_line))
                elif not remote.download(origin, source_path(filename, digest), part, on_line):
                    continue
            except (OSError, remote.RemoteError) as e:
                error = e
                continue
            if sha256_of(part) == digest:
                part.rename(archive)
                return True
            where = origin if isinstance(origin, str) else origin.url
            error = SourceError(f"{filename} from {where} does not match the sha256 of the manifest")
    part.unlink(missing_ok=True)
    with contextlib.suppress(OSError):  # not to leave it empty
        archive.parent.rmdir()
    if error:
        msg = f"{filename} is not in the source cache, and cannot be downloaded: {error}"
        raise SourceError(msg) from error
    return False


def has_cached_source(project: Project, filename: str, digest: str) -> bool:
    """Return whether the source cache has the archive `filename`, and raise when it has other contents than
    `digest`.
    """
    archive = project.source_cache / digest / filename
    if not archive.exists():
        return False
    os.utime(archive.parent)  # used, see xcraft clean --keep-days
    if sha256_of(archive) != digest:
        msg = f"{filename} in the source cache does not match the sha256 of the manifest"
        raise SourceError(msg)
    return True


def fetch_sources(project: Project, name: str, manifest: dict[str, str] | None = None) -> list[str]:
    """Put the archives of `name` in the source cache, downloaded when missing, and return those found nowhere: those
    of its `manifest`, the filename to SHA-256 mapping of its ``sources``, read from it unless given.
    """
    if manifest is None:
        path = project.package_dirs[name] / "sources"
        manifest = read_manifest(path) if path.exists() else {}
    return [
        filename
        for filename, digest in manifest.items()
        if not has_cached_source(project, filename, digest) and not _fetch_source(project, name, filename, digest)
    ]
