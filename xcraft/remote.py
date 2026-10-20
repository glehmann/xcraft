"""The shared storage: public reads over http(s), authenticated writes with S3.

It has the source archives, as ``sources/<sha256>/<filename>`` like the local
source cache, and the builds, as ``builds/<arch>/<package>/<key>/`` trees.
Garage, like other S3 storages, stores identical files once: copies cost
nothing, so builds are stored as their files, for the published repositories
to be copies of them.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import functools
import ipaddress
import json
import math
import os
import socket
import tempfile
import urllib.error
import urllib.request
from html import escape
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

from xcraft.display import download_progress
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from mypy_boto3_s3 import S3Client

    from xcraft.project import Remote


class RemoteError(RuntimeError):
    """Raised when the shared storage cannot be used."""


def _url(storage: Remote, path: str) -> str:
    return f"{storage.url.rstrip('/')}/{path}"


def on_this_machine(storage: Remote) -> bool:
    """Return whether the storage is on this machine, like local-s3.sh's: its URLs resolve to loopback addresses."""

    def loopback(url: str) -> bool:
        try:
            addresses = {info[4][0] for info in socket.getaddrinfo(urlparse(url).hostname or "", None)}
        except (OSError, UnicodeError):
            return False
        return bool(addresses) and all(ipaddress.ip_address(a).is_loopback for a in addresses)

    return loopback(storage.url) and loopback(storage.s3_endpoint or storage.url)


def exists(storage: Remote, path: str) -> bool:
    """Return whether the shared storage has `path`."""
    try:
        with urllib.request.urlopen(urllib.request.Request(_url(storage, path), method="HEAD")):  # noqa: S310
            return True
    except OSError as e:
        return _absent_or_raise(storage, path, e)


def download(storage: Remote, path: str, dest: Path, on_line: Callable[[str], None] = lambda _: None) -> bool:
    """Download `path` to `dest`, and return whether the shared storage has it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.part")
    try:
        urllib.request.urlretrieve(_url(storage, path), part, reporthook=download_progress(on_line))  # noqa: S310
    except OSError as e:
        part.unlink(missing_ok=True)
        return _absent_or_raise(storage, path, e)
    part.rename(dest)
    return True


def _absent_or_raise(storage: Remote, path: str, error: OSError) -> bool:
    """Return False when `error` is that the storage does not have `path`, or raise it as a RemoteError."""
    if isinstance(error, urllib.error.HTTPError) and error.code == 404:  # noqa: PLR2004
        return False
    msg = f"{_url(storage, path)}: {error}"
    raise RemoteError(msg) from error


@functools.cache
def _s3(endpoint: str, region: str, access_key: str) -> S3Client:  # noqa: ARG001, access_key: one client per credentials
    import boto3  # noqa: PLC0415, only for the writes, and slow to import

    return boto3.client("s3", endpoint_url=endpoint, region_name=region)


@contextlib.contextmanager
def _writing(storage: Remote, action: str) -> Generator[S3Client]:
    """Return the S3 client of `storage`, reporting the S3 errors of `action` as RemoteError."""
    if not (storage.s3_endpoint and storage.bucket):
        msg = "pushing needs the s3_endpoint and bucket of the storage, in xcraft.toml"
        raise RemoteError(msg)
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        msg = "pushing needs the S3 credentials, in AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
        raise RemoteError(msg)
    try:
        yield _s3(storage.s3_endpoint, storage.region, os.environ["AWS_ACCESS_KEY_ID"])
    except RemoteError:
        raise
    except Exception as e:  # botocore has many exception types, all to report the same way
        msg = f"cannot {action}: {e}"
        raise RemoteError(msg) from e


def upload(storage: Remote, path: str, file: Path) -> None:
    """Upload `file` as `path`, with the S3 credentials of the environment."""
    with _writing(storage, f"upload {path}") as s3:
        s3.upload_file(str(file), storage.bucket, path)


def copy(storage: Remote, src: str, dest: str) -> None:
    """Copy `src` to `dest` in the storage, without transferring it."""
    with _writing(storage, f"copy {src} to {dest}") as s3:
        s3.copy_object(Bucket=storage.bucket, Key=dest, CopySource={"Bucket": storage.bucket, "Key": src})


def listing(storage: Remote, prefix: str) -> list[str]:
    """Return the paths under `prefix`, with the S3 credentials, as the public reads cannot list."""
    with _writing(storage, f"list {prefix}") as s3:
        pages = s3.get_paginator("list_objects_v2").paginate(Bucket=storage.bucket, Prefix=prefix)
        return [o["Key"] for page in pages for o in page.get("Contents", [])]


def delete(storage: Remote, paths: list[str]) -> None:
    """Delete `paths` from the storage."""
    with _writing(storage, f"delete {len(paths)} files") as s3:
        for i in range(0, len(paths), 1000):  # the most S3 deletes at once
            s3.delete_objects(Bucket=storage.bucket, Delete={"Objects": [{"Key": p} for p in paths[i : i + 1000]]})


MANIFEST = "manifest.json"
"""The files of a tree in the storage, with their SHA-256, uploaded last: a tree without it is incomplete."""


def tree_exists(storage: Remote, prefix: str) -> bool:
    """Return whether the storage has the complete tree `prefix`."""
    return exists(storage, f"{prefix}/{MANIFEST}")


def download_tree(storage: Remote, prefix: str, dest: Path, on_line: Callable[[str], None] = lambda _: None) -> bool:
    """Download the tree `prefix` as the new directory `dest`, and return whether the storage has it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # next to it, to move it there
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".download-") as tmp:
        tree = Path(tmp) / "tree"
        if not download(storage, f"{prefix}/{MANIFEST}", Path(tmp) / MANIFEST):
            return False
        for name, digest in json.loads((Path(tmp) / MANIFEST).read_text()).items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                msg = f"{prefix}/{MANIFEST} has a path out of its tree: {name}"
                raise RemoteError(msg)
            if not download(storage, f"{prefix}/{name}", tree / name, on_line) or sha256_of(tree / name) != digest:
                msg = f"{prefix}/{name} is missing or corrupted in the storage"
                raise RemoteError(msg)
        tree.rename(dest)
    return True


def upload_tree(storage: Remote, prefix: str, src: Path, names: list[str]) -> dict[str, str]:
    """Upload the entries `names` of the directory `src` as the tree `prefix`, and return its files' SHA-256."""
    files = sorted(f for name in names for f in [src / name, *(src / name).rglob("*")] if f.is_file())
    manifest = {f.relative_to(src).as_posix(): sha256_of(f) for f in files}
    for name in manifest:
        upload(storage, f"{prefix}/{name}", src / name)
    write_manifest(storage, prefix, manifest)
    return manifest


def read_manifest(storage: Remote, prefix: str) -> dict[str, str]:
    """Return the SHA-256 of the files of the tree `prefix`, by path in it, or nothing without its manifest."""
    with tempfile.TemporaryDirectory(prefix="xcraft-manifest-") as tmp:
        if not download(storage, f"{prefix}/{MANIFEST}", Path(tmp) / MANIFEST):
            return {}
        return json.loads((Path(tmp) / MANIFEST).read_text())


def write_manifest(storage: Remote, prefix: str, manifest: dict[str, str]) -> None:
    """Write the manifest of the tree `prefix`, which completes it."""
    with _writing(storage, f"write {prefix}/{MANIFEST}") as s3:
        s3.put_object(
            Bucket=storage.bucket,
            Key=f"{prefix}/{MANIFEST}",
            Body=json.dumps(manifest, indent=1, sort_keys=True).encode(),
            ContentType="application/json",
        )


INDEX = "index.html"
"""The page of a directory, which the storage serves for it, but does not make."""

_Entry = tuple[int, dt.datetime]
"""Size and date of a file."""
_Dirs = dict[str, tuple[int, dt.datetime | None]]
"""Subdirectories, with the size of all that is below each, and the date of the newest file, if known."""


def _objects(s3: S3Client, bucket: str, prefix: str, *, below: bool) -> tuple[set[str], dict[str, _Entry]]:
    """Return the subdirectories and files of `prefix`, or with `below`, all the files below it, but the pages."""
    paginator = s3.get_paginator("list_objects_v2")
    pages = (
        paginator.paginate(Bucket=bucket, Prefix=prefix)
        if below
        else paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
    )
    dirs, files = set(), {}
    for page in pages:
        dirs |= {p["Prefix"].removeprefix(prefix).rstrip("/") for p in page.get("CommonPrefixes", [])}
        files |= {
            o["Key"].removeprefix(prefix): (o["Size"], o["LastModified"])
            for o in page.get("Contents", [])
            if o["Key"].rsplit("/", 1)[-1] != INDEX
        }
    return dirs, files


_STYLE = """
<style>
body { font-family: monospace }
td, th { padding: 0.1em 1em; text-align: left }
td:nth-child(2), th:nth-child(2) { text-align: right }
th { cursor: pointer }
</style>"""

# sorts the rows by the data-k of the cells of a column, at a click on its header, then in reverse
_SORT = """<script>
document.querySelectorAll("th").forEach((th, i) => th.onclick = () => {
  const order = th.dataset.order = th.dataset.order === "1" ? "-1" : "1", body = document.querySelector("tbody");
  body.append(...[...body.rows].sort((a, b) =>
    order * a.cells[i].dataset.k.localeCompare(b.cells[i].dataset.k, undefined, {numeric: true})));
});
</script>"""


def _total(dirs: _Dirs, files: dict[str, _Entry]) -> tuple[int, dt.datetime | None]:
    """Return the size of all that is in a directory, and the date of its newest file."""
    entries = [*dirs.values(), *files.values()]
    return sum(size for size, _ in entries), max((date for _, date in entries if date), default=None)


def _stored_total(s3: S3Client, bucket: str, prefix: str) -> tuple[int, dt.datetime | None]:
    """Return the size of all that is below `prefix`, and the date of its newest file, which its page keeps."""
    try:
        metadata = s3.head_object(Bucket=bucket, Key=prefix + INDEX)["Metadata"]
    except s3.exceptions.ClientError:  # no page yet
        return 0, None
    newest = metadata.get("newest")
    return int(metadata.get("size", 0)), dt.datetime.fromisoformat(newest) if newest else None


_UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def _human_size(size: int) -> str:
    exponent = min(int(math.log(size, 1024)), len(_UNITS) - 1) if size else 0
    return f"{size / 1024**exponent:.1f} {_UNITS[exponent]}" if exponent else f"{size} B"


def _row(name: str, href: str, size: int, date: dt.datetime | None) -> str:
    """Return the row of a file or a directory, with the sort keys of its cells."""
    key, shown = (f"{date:%Y-%m-%dT%H:%M:%S}", f"{date:%Y-%m-%d %H:%M}") if date else ("", "")
    return (
        f'<tr><td data-k="{escape(name)}"><a href="{quote(href)}">{escape(href)}</a></td>'
        f'<td data-k="{size}">{_human_size(size)}</td><td data-k="{key}">{shown}</td></tr>'
    )


def _page(prefix: str, dirs: _Dirs, files: dict[str, _Entry]) -> str:
    rows = [_row(d, f"{d}/", *total) for d, total in sorted(dirs.items())]
    rows += [_row(f, f, *entry) for f, entry in sorted(files.items())]
    parent = '<tr><td><a href="../">../</a></td></tr>' if prefix else ""
    title = escape(f"/{prefix}")
    return (
        f'<!DOCTYPE html>\n<html><head><meta charset="utf-8"><title>{title}</title>{_STYLE}</head><body>\n'
        f"<h1>{title}</h1>\n<table>\n<thead><tr><th>Name</th><th>Size</th><th>Date</th></tr>{parent}</thead>\n"
        "<tbody>\n" + "\n".join(rows) + f"\n</tbody>\n</table>\n{_SORT}\n</body></html>\n"
    )


def update_pages(storage: Remote, prefixes: list[str]) -> int:
    """Write the pages of the directories under the directories `prefixes`, and of their parents; return how many.

    The directories are the prefixes ending with ``/``, and ``""`` is the root.
    """
    pages: dict[str, tuple[_Dirs, dict[str, _Entry]]] = {}
    parents: set[str] = set()
    with _writing(storage, "make the directory pages") as s3:
        # those under another one are in its listing
        for prefix in sorted(p for p in set(prefixes) if not any(p != q and p.startswith(q) for q in prefixes)):
            pages.setdefault(prefix, ({}, {}))
            for path, (size, date) in _objects(s3, storage.bucket, prefix, below=True)[1].items():
                *parts, name = path.split("/")
                for i, part in enumerate(parts):
                    dirs = pages.setdefault(prefix + "".join(f"{p}/" for p in parts[:i]), ({}, {}))[0]
                    below, newest = dirs.get(part, (0, None))
                    dirs[part] = (below + size, max(filter(None, (newest, date))))
                pages.setdefault(prefix + "".join(f"{p}/" for p in parts), ({}, {}))[1][name] = (size, date)
            above = prefix.rstrip("/").split("/")[:-1] if prefix else []
            parents |= {"", *("".join(f"{p}/" for p in above[: i + 1]) for i in range(len(above)))}
        # the parents are only listed, the deepest first, with the totals of their subdirectories' pages
        for parent in sorted(parents - pages.keys(), key=lambda p: -p.count("/")):
            dirs, files = _objects(s3, storage.bucket, parent, below=False)
            pages[parent] = (
                {
                    d: _total(*pages[f"{parent}{d}/"])
                    if f"{parent}{d}/" in pages
                    else _stored_total(s3, storage.bucket, f"{parent}{d}/")
                    for d in dirs
                },
                files,
            )
        for prefix, (dirs, files) in pages.items():
            size, newest = _total(dirs, files)
            s3.put_object(
                Bucket=storage.bucket,
                Key=prefix + INDEX,
                Body=_page(prefix, dirs, files).encode(),
                ContentType="text/html; charset=utf-8",
                # revalidated by the browsers, by their ETag: they change with each push, under the same name
                CacheControl="no-cache",
                Metadata={"size": str(size), **({"newest": newest.isoformat()} if newest else {})},
            )
    return len(pages)
