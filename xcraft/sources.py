"""Source archives: the ``sources`` manifest of a package, and the local source cache.

Archives are not committed with the specs. A package's ``sources`` file lists
them as ``<sha256>  <filename>`` lines, and the cache stores them as
``<cache_dir>/<sha256>/<filename>``.
"""

from __future__ import annotations

import hashlib
import shutil
import urllib.parse
from typing import TYPE_CHECKING

from xcraft.project import write_atomically

if TYPE_CHECKING:
    from pathlib import Path


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
