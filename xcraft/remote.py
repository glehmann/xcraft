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
from concurrent.futures import ThreadPoolExecutor
from html import escape
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse

from pydantic import BaseModel, field_validator

from xcraft import process
from xcraft.display import download_progress
from xcraft.process import log
from xcraft.signing import Signer, SigningError, dev_signer
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from mypy_boto3_s3 import S3Client

    from xcraft.project import Project, Remote


class RemoteError(RuntimeError):
    """Raised when the shared storage cannot be used."""


def _url(storage: Remote, path: str) -> str:
    return f"{storage.url.rstrip('/')}/{path}"


def _s3_url(storage: Remote, path: str) -> str:
    return f"s3://{storage.bucket}/{path}"


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
    log.debug("checking %s", _url(storage, path))
    try:
        with urllib.request.urlopen(urllib.request.Request(_url(storage, path), method="HEAD")):  # noqa: S310
            return True
    except OSError as e:
        return _absent_or_raise(storage, path, e)


_REQUESTS = 16
"""The requests at the same time to the storage, many of which are only small: the time is in their round trips."""


def existing(storage: Remote, paths: list[str]) -> set[str]:
    """Return those of `paths` the shared storage has, asking for them concurrently."""
    with ThreadPoolExecutor(_REQUESTS) as pool:
        return {path for path, found in zip(paths, pool.map(lambda p: exists(storage, p), paths), strict=True) if found}


def download(storage: Remote, path: str, dest: Path, on_line: Callable[[str], None] = lambda _: None) -> bool:
    """Download `path` to `dest`, and return whether the shared storage has it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.part")
    try:
        process.download(_url(storage, path), part, download_progress(on_line))
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
def _s3(endpoint: str, region: str, access_key: str, secret_key: str, session_token: str) -> S3Client:
    import boto3  # noqa: PLC0415, only for the writes, and slow to import

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token or None,
    )


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
        yield _s3(
            storage.s3_endpoint,
            storage.region,
            os.environ["AWS_ACCESS_KEY_ID"],
            os.environ["AWS_SECRET_ACCESS_KEY"],
            os.environ.get("AWS_SESSION_TOKEN", ""),
        )
    except Exception as e:
        # those of boto3 and botocore, imported with it, not those of xcraft
        from boto3.exceptions import Boto3Error  # noqa: PLC0415
        from botocore.exceptions import BotoCoreError, ClientError  # noqa: PLC0415

        if not isinstance(e, Boto3Error | BotoCoreError | ClientError):
            raise
        msg = f"cannot {action}: {e}"
        raise RemoteError(msg) from e


def upload(storage: Remote, path: str, file: Path) -> None:
    """Upload `file` as `path`, with the S3 credentials of the environment."""
    log.debug("uploading %s to %s", file, _s3_url(storage, path))
    with _writing(storage, f"upload {path}") as s3:
        s3.upload_file(str(file), storage.bucket, path)


def copy(storage: Remote, src: str, dest: str) -> None:
    """Copy `src` to `dest` in the storage, without transferring it."""
    log.debug("copying %s to %s", _s3_url(storage, src), _s3_url(storage, dest))
    with _writing(storage, f"copy {src} to {dest}") as s3:
        s3.copy_object(Bucket=storage.bucket, Key=dest, CopySource={"Bucket": storage.bucket, "Key": src})


def copy_checked(storage: Remote, src: str, dest: str, digest: str) -> None:
    """Copy a file, checking its bytes before a publisher signs a new manifest."""
    copy(storage, src, dest)
    with tempfile.TemporaryDirectory(prefix="xcraft-copy-") as tmp:
        file = Path(tmp) / "file"
        if not download(storage, dest, file) or sha256_of(file) != digest:
            msg = f"{src} is missing or corrupted in the storage"
            raise RemoteError(msg)


def listing(storage: Remote, prefix: str) -> list[str]:
    """Return the files below the directory `prefix`, relative to it, but the pages of the directories, with the S3
    credentials, as the public reads cannot list.
    """  # noqa: D205
    with _writing(storage, f"list {prefix}") as s3:
        return list(_objects(s3, storage.bucket, prefix, below=True)[1])


def delete(storage: Remote, paths: list[str]) -> None:
    """Delete `paths` from the storage."""
    for path in paths:
        log.debug("deleting %s", _s3_url(storage, path))
    with _writing(storage, f"delete {len(paths)} files") as s3:
        for i in range(0, len(paths), 1000):  # the most S3 deletes at once
            s3.delete_objects(Bucket=storage.bucket, Delete={"Objects": [{"Key": p} for p in paths[i : i + 1000]]})


MANIFEST = "manifest.json"
"""The files of a tree in the storage, with their SHA-256, uploaded last: a tree without it is incomplete."""
SIGNATURE = f"{MANIFEST}.asc"


class TreeManifest(BaseModel):
    """Validate paths and digests from a signed remote manifest."""

    files: dict[str, str]

    @field_validator("files")
    @classmethod
    def valid_files(cls, files: dict[str, str]) -> dict[str, str]:
        """Reject paths outside the tree and malformed digests."""
        for name, digest in files.items():
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.as_posix() != name:
                msg = f"invalid tree path: {name}"
                raise ValueError(msg)
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):  # noqa: PLR2004
                msg = f"invalid SHA-256: {name}"
                raise ValueError(msg)
        return files


def _payload(prefix: str, manifest: bytes) -> bytes:
    return prefix.encode() + b"\n" + manifest


def _manifest(storage: Remote, prefix: str, trusted: Signer | None) -> dict[str, str] | None:
    fingerprints = {*storage.build_keys, *([trusted.key.upper()] if trusted else [])}
    if not fingerprints:
        return None
    with tempfile.TemporaryDirectory(prefix="xcraft-manifest-") as tmp:
        work = Path(tmp)
        manifest, signature = work / MANIFEST, work / SIGNATURE
        if not download(storage, f"{prefix}/{MANIFEST}", manifest) or not download(
            storage, f"{prefix}/{SIGNATURE}", signature
        ):
            return None
        payload = work / "payload"
        payload.write_bytes(_payload(prefix, manifest.read_bytes()))
        keyring = work / "keys"
        keyring.mkdir(mode=0o700)
        verifier = Signer(key="", gnupghome=keyring)
        if trusted:
            key = work / "local.asc"
            key.write_text(trusted.public_key())
            verifier.gpg("--import", str(key))
        for fingerprint in storage.build_keys:
            key = work / "key.asc"
            if not download(storage, f"keys/{fingerprint}.asc", key):
                continue
            try:
                shown = verifier.gpg("--with-colons", "--show-keys", str(key))
                shown_fingerprints = [line.split(":")[9] for line in shown.splitlines() if line.startswith("fpr:")]
                if not shown_fingerprints or shown_fingerprints[0] != fingerprint:
                    continue
                verifier.gpg("--import", str(key))
            except SigningError:
                continue
        try:
            status = verifier.gpg("--status-fd", "1", "--verify", str(signature), str(payload))
            if not any(
                line.startswith("[GNUPG:] VALIDSIG ")
                and (line.split()[2] in fingerprints or line.split()[-1] in fingerprints)
                for line in status.splitlines()
            ):
                return None
            return TreeManifest(files=json.loads(manifest.read_bytes())).files
        except (SigningError, ValueError):
            log.warning("ignoring invalid remote tree %s", prefix)
            return None


def tree_exists(storage: Remote, prefix: str, *, trusted: Signer | None = None) -> bool:
    """Return whether the storage has the complete tree `prefix`."""
    return _manifest(storage, prefix, trusted) is not None


def storages(project: Project) -> list[Remote]:
    """Search main before dev, with the same configured trust regardless of the serving bucket."""
    keys = sorted({key for storage in project.config.remote.values() for key in storage.build_keys})
    return [
        storage.model_copy(update={"build_keys": keys})
        for name in ("main", "dev")
        if (storage := project.config.remote.get(name))
    ]


def tree_available(project: Project, prefix: str) -> bool:
    """Check signed manifests in main, then dev, without downloading tree contents."""
    error = None
    for storage in storages(project):
        try:
            if tree_exists(storage, prefix, trusted=dev_signer(project)):
                return True
        except RemoteError as e:
            error = e
    if error:
        raise error
    return False


def fetch_tree(project: Project, prefix: str, dest: Path, on_line: Callable[[str], None] = lambda _: None) -> bool:
    """Download a trusted tree from main or dev, checking its signature before its contents."""
    error = None
    for storage in storages(project):
        try:
            if download_tree(storage, prefix, dest, on_line, trusted=dev_signer(project)):
                return True
        except RemoteError as e:
            error = e
    if error:
        raise error
    return False


def download_tree(
    storage: Remote,
    prefix: str,
    dest: Path,
    on_line: Callable[[str], None] = lambda _: None,
    *,
    trusted: Signer | None = None,
) -> bool:
    """Download the tree `prefix` as the new directory `dest`, and return whether the storage has it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    # next to it, to move it there
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".download-") as tmp:
        tree = Path(tmp) / "tree"
        manifest = _manifest(storage, prefix, trusted)
        if manifest is None:
            return False
        tree.mkdir()
        for name, digest in manifest.items():
            if not download(storage, f"{prefix}/{name}", tree / name, on_line) or sha256_of(tree / name) != digest:
                msg = f"{prefix}/{name} is missing or corrupted in the storage"
                raise RemoteError(msg)
        tree.rename(dest)
    return True


def upload_tree(storage: Remote, prefix: str, src: Path, names: list[str], *, signer: Signer | None) -> dict[str, str]:
    """Upload the entries `names` of the directory `src` as the tree `prefix`, and return its files' SHA-256."""
    if signer is None:
        msg = "no key to sign uploads: run xcraft key create, configure [sign], or use --key"
        raise RemoteError(msg)
    files = sorted(f for name in names for f in [src / name, *(src / name).rglob("*")] if f.is_file())
    manifest = {f.relative_to(src).as_posix(): sha256_of(f) for f in files}
    for name in manifest:
        upload(storage, f"{prefix}/{name}", src / name)
    write_manifest(storage, prefix, manifest, signer=signer)
    return manifest


def read_manifest(storage: Remote, prefix: str, *, trusted: Signer | None = None) -> dict[str, str]:
    """Return the SHA-256 of the files of the tree `prefix`, by path in it, or nothing without its manifest."""
    return _manifest(storage, prefix, trusted) or {}


def write_manifest(storage: Remote, prefix: str, manifest: dict[str, str], *, signer: Signer | None) -> None:
    """Write the manifest of the tree `prefix`, which completes it."""
    if signer is None:
        msg = "no key to sign uploads: run xcraft key create, configure [sign], or use --key"
        raise RemoteError(msg)
    TreeManifest(files=manifest)
    with tempfile.TemporaryDirectory(prefix="xcraft-sign-") as tmp:
        work = Path(tmp)
        file = work / MANIFEST
        file.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        payload = work / "payload"
        payload.write_bytes(_payload(prefix, file.read_bytes()))
        signature = signer.sign_file(payload)
        key = work / "key.asc"
        key.write_text(signer.public_key())
        upload(storage, f"keys/{signer.key}.asc", key)
        upload(storage, f"{prefix}/{SIGNATURE}", signature)
        upload(storage, f"{prefix}/{MANIFEST}", file)


INDEX = "index.html"
"""The page of a directory, which the storage serves for it, but does not make."""

_Entry = tuple[int, dt.datetime]
"""Size and date of a file."""
_Dirs = dict[str, tuple[int, dt.datetime | None]]
"""Subdirectories, with the size of all that is below each, and the date of the newest file, if known."""


def _objects(s3: S3Client, bucket: str, prefix: str, *, below: bool) -> tuple[set[str], dict[str, _Entry]]:
    """Return the subdirectories and files of `prefix`, or with `below`, all the files below it, but the pages."""
    log.debug("listing s3://%s/%s", bucket, prefix)
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
:root { color-scheme: light dark }
body { margin: 0 auto; padding: 2.5rem 1.5rem 6rem; max-width: 64rem;
       font-family: system-ui, sans-serif; font-size: .9rem; color: #22222c; background: #fff }
img { display: block; width: 10rem; margin-bottom: 1.5rem }
h1 { font-size: 1.1rem; font-weight: 600; margin: 0 0 1.25rem }
a { color: inherit; text-decoration: none }
table { border-collapse: collapse; width: 100% }
th { font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .08em;
     color: #8b8b94; cursor: pointer; user-select: none }
td { border-top: 1px solid #ececf1; color: #6d6d76 }
th, td { padding: .4rem 0; text-align: left }
th + th, td + td { padding-left: 1.5rem }
th:nth-child(n+2), td:nth-child(n+2) { text-align: right; white-space: nowrap }
td:first-child { color: inherit }
tbody tr:hover { background: #f6f6f9 }
tbody a:hover { text-decoration: underline }
@media (prefers-color-scheme: dark) {
  body { color: #e4e4ec; background: #14141a }
  th { color: #9797a3 }
  td { border-color: #26262f; color: #ababb6 }
  tbody tr:hover { background: #1d1d25 }
}
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
    log.debug("checking s3://%s/%s%s", bucket, prefix, INDEX)
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


def _page(prefix: str, dirs: _Dirs, files: dict[str, _Entry], logo: str | None) -> str:
    rows = [_row(d, f"{d}/", *total) for d, total in sorted(dirs.items())]
    rows += [_row(f, f, *entry) for f, entry in sorted(files.items())]
    parent = '<tr><td><a href="../">../</a></td></tr>' if prefix else ""
    title = escape(f"/{prefix}")
    img = f'<img src="{escape(logo)}" alt="">\n' if logo else ""
    return (
        f'<!DOCTYPE html>\n<html><head><meta charset="utf-8"><title>{title}</title>{_STYLE}</head><body>\n'
        f"{img}<h1>{title}</h1>\n<table>\n<thead><tr><th>Name</th><th>Size</th><th>Date</th></tr>{parent}</thead>\n"
        "<tbody>\n" + "\n".join(rows) + f"\n</tbody>\n</table>\n{_SORT}\n</body></html>\n"
    )


def update_pages(project: Project, storage: Remote, prefixes: list[str]) -> int:
    """Write the pages of the directories under the directories `prefixes`, and of their parents; return how many.

    The directories are the prefixes ending with ``/``, and ``""`` is the root.
    """
    pages: dict[str, tuple[_Dirs, dict[str, _Entry]]] = {}
    parents: set[str] = set()
    with _writing(storage, "make the directory pages") as s3:
        if _objects(s3, storage.bucket, "keys/", below=True)[1]:
            prefixes = [*prefixes, "keys/"]
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

        def write(prefix: str) -> None:
            dirs, files = pages[prefix]
            size, newest = _total(dirs, files)
            log.debug("writing %s", _s3_url(storage, prefix + INDEX))
            s3.put_object(
                Bucket=storage.bucket,
                Key=prefix + INDEX,
                Body=_page(prefix, dirs, files, project.config.logo).encode(),
                ContentType="text/html; charset=utf-8",
                # revalidated by the browsers, by their ETag: they change with each push, under the same name
                CacheControl="no-cache",
                Metadata={"size": str(size), **({"newest": newest.isoformat()} if newest else {})},
            )

        with ThreadPoolExecutor(_REQUESTS) as pool:
            list(pool.map(write, pages))
    return len(pages)
