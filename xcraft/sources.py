"""Source archives: the ``sources`` manifest of a package, the local source cache, and their downloads.

Archives are not committed with the specs. A package's ``sources`` file lists
them as ``<sha256>  <filename>`` lines, and the cache stores them as
``<cache_dir>/<sha256>/<filename>``. Missing ones come from the remote storage,
or else from the URLs of the spec.
"""

from __future__ import annotations

import contextlib
import hashlib
import shutil
import urllib.parse
from typing import TYPE_CHECKING

from xcraft import process
from xcraft.display import activity, download_progress
from xcraft.project import write_atomically

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


def sha256_of(path: Path) -> str:
    """Return the SHA-256 hex digest of a file."""
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def read_manifest(path: Path) -> dict[str, str]:
    """Return the filename to SHA-256 mapping of a ``sources`` manifest."""
    return {name: digest for digest, name in (line.split(maxsplit=1) for line in path.read_text().splitlines() if line)}


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


class SourceError(RuntimeError):
    """Raised when an archive cannot be downloaded, or does not match its manifest."""


def source_path(filename: str, digest: str) -> str:
    """Return the path of a source archive in the remote storage."""
    return f"sources/{digest}/{filename}"


def _fetch_source(project: Project, name: str, filename: str, digest: str) -> bool:
    """Download an archive of `name` into the source cache, and return whether it was found.

    It comes from the remote storage, or else from the source of the spec of `name` whose URL is its own.
    """
    from xcraft import remote  # noqa: PLC0415, which uses sha256_of()

    archive = project.source_cache / digest / filename
    part = archive.with_name(f"{filename}.part")
    part.parent.mkdir(parents=True, exist_ok=True)
    urls = (s for s in project.packages[name].sources if "://" in s and archive_name(s) == filename)
    url, origin, error = next(urls, ""), "", None
    with activity(f"downloading {filename}") as on_line:
        for bucket in ("main", "dev"):
            if source := project.config.remote.get(bucket):
                try:
                    if remote.download(source, source_path(filename, digest), part, on_line):
                        origin = "the remote storage"
                        break
                except remote.RemoteError as e:
                    error = e
        if not origin and url:
            try:
                process.download(url, part, download_progress(on_line))
                origin = url
            except OSError as e:
                error = e
    if not origin:
        part.unlink(missing_ok=True)
        with contextlib.suppress(OSError):  # not to leave it empty
            part.parent.rmdir()
        if error:
            msg = f"{filename} is not in the source cache, and cannot be downloaded: {error}"
            raise SourceError(msg) from error
        return False
    if sha256_of(part) != digest:
        part.unlink()
        msg = f"{filename} from {origin} does not match the sha256 of the manifest"
        raise SourceError(msg)
    part.rename(archive)
    return True


def fetch_sources(project: Project, name: str) -> list[str]:
    """Put the archives of `name` in the source cache, downloaded when missing, and return those found nowhere."""
    manifest = project.package_dirs[name] / "sources"
    return [
        filename
        for filename, digest in (read_manifest(manifest).items() if manifest.exists() else [])
        if not cached_source(project, filename, digest) and not _fetch_source(project, name, filename, digest)
    ]


def cached_source(project: Project, filename: str, digest: str) -> bool:
    """Check cached archives against the committed source manifest too."""
    archive = project.source_cache / digest / filename
    if not archive.exists():
        return False
    if sha256_of(archive) != digest:
        msg = f"{filename} in the source cache does not match the sha256 of the manifest"
        raise SourceError(msg)
    return True
