"""The shared storage: public reads over http(s), authenticated writes with S3.

It has the published repositories and ISOs, in ``repos/`` and ``isos/``, and,
in ``.xcraft/``, what xcraft keeps for itself: the source archives, as
``.xcraft/sources/<sha256>/<filename>`` like the local source cache, and the
builds, as ``.xcraft/builds/<arch>/<package>/<key>/`` trees, with the signed
builds, the compiler caches, the public keys and the ISOs of the checkouts.
Garage, like other S3 storages, stores identical files once: copies cost
nothing, so builds are stored as their files, for the published repositories
to be copies of them.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import email.message
import functools
import ipaddress
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

import urllib3
from pydantic import BaseModel, field_validator

from xcraft import process
from xcraft.display import download_progress
from xcraft.process import log
from xcraft.project import INTERNAL
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


@functools.cache
def _pool() -> urllib3.PoolManager:
    """Return the connections to the storages, reused: a new one takes several round trips, with TLS."""
    return urllib3.PoolManager(maxsize=MANY)


def _pooled(url: str) -> bool:
    """Return whether `url` is requested through _pool(): not through a proxy, which only urllib knows."""
    parsed = urlparse(url)
    return parsed.scheme not in urllib.request.getproxies() or bool(urllib.request.proxy_bypass(parsed.hostname or ""))


def _request(method: str, url: str) -> urllib3.BaseHTTPResponse:
    """Request `url`, failing like urllib, with an OSError, an HTTPError for an HTTP error."""
    try:
        response = _pool().request(method, url, preload_content=False)
    except urllib3.exceptions.HTTPError as e:
        raise urllib.error.URLError(e) from e
    if response.status >= 400:  # noqa: PLR2004
        response.drain_conn()
        response.release_conn()
        raise urllib.error.HTTPError(url, response.status, response.reason or "", email.message.Message(), None)
    return response


def exists(storage: Remote, path: str) -> bool:
    """Return whether the shared storage has `path`."""
    url = _url(storage, path)
    log.debug("checking %s", url)
    try:
        if _pooled(url):
            _request("HEAD", url).release_conn()
        else:
            urllib.request.urlopen(urllib.request.Request(url, method="HEAD")).close()  # noqa: S310
    except OSError as e:
        return _absent_or_raise(storage, path, e)
    return True


REQUESTS = 16
"""The requests at the same time to the storage, many of which are only small: the time is in their round trips."""
MANY = 64
"""The requests at the same time for the trees of many small files, like the compiler caches: the storage takes ~70ms
to write each, which more requests at once hide."""
SINGLE = 8 * 2**20
"""The size of the files uploaded in one request, rather than in parts: those smaller than the parts of boto3."""


def existing(storage: Remote, paths: list[str]) -> set[str]:
    """Return those of `paths` the shared storage has, asking for them concurrently."""
    with ThreadPoolExecutor(REQUESTS) as pool:
        return {path for path, found in zip(paths, pool.map(lambda p: exists(storage, p), paths), strict=True) if found}


def download(storage: Remote, path: str, dest: Path, on_line: Callable[[str], None] = lambda _: None) -> bool:
    """Download `path` to `dest`, and return whether the shared storage has it."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(f"{dest.name}.part")
    try:
        if _pooled(url := _url(storage, path)):
            _get(url, part, download_progress(on_line))
        else:
            process.download(url, part, download_progress(on_line))
    except OSError as e:
        part.unlink(missing_ok=True)
        return _absent_or_raise(storage, path, e)
    part.rename(dest)
    return True


_BLOCK = 1 << 16


def _get(url: str, dest: Path, report: Callable[[int, int, int], None]) -> None:
    """Download `url` to `dest` like process.download(), over the connections of _pool()."""
    log.debug("downloading %s to %s", url, dest)
    response = _request("GET", url)
    try:
        total = int(response.headers.get("Content-Length", -1))
        with dest.open("wb") as file:
            for blocks, chunk in enumerate(response.stream(_BLOCK, decode_content=False), 1):
                file.write(chunk)
                report(blocks, _BLOCK, total)
    except urllib3.exceptions.HTTPError as e:
        raise urllib.error.URLError(e) from e
    finally:
        response.release_conn()


def _absent_or_raise(storage: Remote, path: str, error: OSError) -> bool:
    """Return False when `error` is that the storage does not have `path`, or raise it as a RemoteError."""
    if isinstance(error, urllib.error.HTTPError) and error.code == 404:  # noqa: PLR2004
        return False
    msg = f"{_url(storage, path)}: {error}"
    raise RemoteError(msg) from error


@functools.cache
def _s3(endpoint: str, region: str, access_key: str, secret_key: str, session_token: str) -> S3Client:
    """Return the client of the storage, made once: a new one opens new connections, for each request."""
    import boto3  # noqa: PLC0415, only for the writes, and slow to import
    from botocore.config import Config  # noqa: PLC0415

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=session_token or None,
        # the concurrent requests, and the parts of the uploads of large files
        config=Config(max_pool_connections=MANY),
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
    transfer(storage, {path: file})


def transfer(
    storage: Remote,
    files: dict[str, str | Path],
    on_line: Callable[[str], None] = lambda _: None,
    *,
    requests: int = REQUESTS,
) -> None:
    """Write the paths of `files`, `requests` at once: copies of the paths of the storage, or uploads of the local
    files.
    """  # noqa: D205
    _forget(storage, list(files))
    with _writing(storage, f"write {len(files)} files") as s3:

        def write(dest: str, src: str | Path) -> str:
            if isinstance(src, Path):
                log.debug("uploading %s to %s", src, _s3_url(storage, dest))
                if src.stat().st_size < SINGLE:  # without the machinery of the uploads in parts
                    s3.put_object(Bucket=storage.bucket, Key=dest, Body=src.read_bytes())
                else:
                    s3.upload_file(str(src), storage.bucket, dest)
            else:
                log.debug("copying %s to %s", _s3_url(storage, src), _s3_url(storage, dest))
                s3.copy_object(Bucket=storage.bucket, Key=dest, CopySource={"Bucket": storage.bucket, "Key": src})
            return dest

        with ThreadPoolExecutor(requests) as pool:
            for dest in pool.map(write, files, files.values()):
                on_line(dest)


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
    _forget(storage, paths)
    with _writing(storage, f"delete {len(paths)} files") as s3:
        for i in range(0, len(paths), 1000):  # the most S3 deletes at once
            s3.delete_objects(Bucket=storage.bucket, Delete={"Objects": [{"Key": p} for p in paths[i : i + 1000]]})


KEYS = f"{INTERNAL}/keys"
"""The public keys of the signed trees, by fingerprint, in the storage."""
MANIFEST = "manifest.asc"
"""The files of a tree in the storage, with their SHA-256, signed in clear, uploaded last: a tree without it is
incomplete. Its text is the path of the tree, then a ``<sha256>  <file>`` line by file, like sha256sum's, sorted."""


class TreeManifest(BaseModel):
    """The files of a tree of the remote storage, with their SHA-256."""

    files: dict[str, str]

    @field_validator("files")
    @classmethod
    def valid_files(cls, files: dict[str, str]) -> dict[str, str]:
        """Reject paths outside the tree and malformed digests."""
        for name, digest in files.items():
            path = PurePosixPath(name)
            # and not changed by the signature in clear, which ignores the spaces ending the lines
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.as_posix() != name
                or name != name.strip()
                or not name.isprintable()
            ):
                msg = f"invalid tree path: {name}"
                raise ValueError(msg)
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):  # noqa: PLR2004
                msg = f"invalid SHA-256: {name}"
                raise ValueError(msg)
        return files


def manifest_text(prefix: str, files: dict[str, str]) -> str:
    """Return the text of the MANIFEST of the tree `prefix`, of `files`, which it signs."""
    return "".join([f"{prefix}\n", *(f"{files[name]}  {name}\n" for name in sorted(files))])


def _parse(prefix: str, text: str) -> dict[str, str]:
    """Return the files of the text of the MANIFEST of `prefix`, or raise ValueError."""
    path, *lines = text.removesuffix("\n").split("\n")
    if path != prefix or not text.endswith("\n") or any(line[64:66] != "  " for line in lines):
        msg = f"not the manifest of {prefix}"
        raise ValueError(msg)
    if len(files := {line[66:]: line[:64] for line in lines}) != len(lines):
        msg = f"files listed twice in the manifest of {prefix}"
        raise ValueError(msg)
    return TreeManifest(files=files).files


_checked: dict[tuple[str, str, frozenset[str]], dict[str, str] | None] = {}
"""The manifests checked, by storage, tree and trusted fingerprints, or None for the trees missing or not trusted,
until written, or the next command."""
_keys: dict[tuple[str, str], str | None] = {}
"""The public keys of the storages, by storage and fingerprint, or None for those they miss or are not."""


def forget() -> None:
    """Forget the manifests and keys read in the storages, for a new command."""
    _checked.clear()
    _keys.clear()


def _forget(storage: Remote, paths: list[str]) -> None:
    """Forget what was read of the manifests and keys of `paths`, which are written."""
    for path in paths:
        if path.startswith(f"{KEYS}/"):
            _keys.pop((storage.url, path.removeprefix(f"{KEYS}/").removesuffix(".asc")), None)
        elif path.endswith(f"/{MANIFEST}"):
            prefix = path.rsplit("/", 1)[0]
            for key in [k for k in _checked if k[:2] == (storage.url, prefix)]:
                _checked.pop(key, None)


def _build_key(storage: Remote, fingerprint: str) -> str | None:
    """Return the public key `fingerprint` of the storage, downloaded once, or None when it has not that key."""
    if (storage.url, fingerprint) not in _keys:
        with tempfile.TemporaryDirectory(prefix="xcraft-key-") as tmp:
            key, found = Path(tmp) / "key.asc", None
            try:
                if download(storage, f"{KEYS}/{fingerprint}.asc", key):
                    shown = Signer(key="", gnupghome=Path(tmp)).gpg("--with-colons", "--show-keys", str(key))
                    if [line.split(":")[9] for line in shown.splitlines() if line.startswith("fpr:")][:1] == [
                        fingerprint
                    ]:
                        found = key.read_text()
            except SigningError:
                pass
        _keys[storage.url, fingerprint] = found
    return _keys[storage.url, fingerprint]


def _manifest(storage: Remote, prefix: str, trusted: Signer | None) -> dict[str, str] | None:
    fingerprints = frozenset({*storage.build_keys, *([trusted.key.upper()] if trusted else [])})
    if not fingerprints:
        return None
    if (storage.url, prefix, fingerprints) not in _checked:
        _checked[storage.url, prefix, fingerprints] = _check(storage, prefix, trusted, fingerprints)
    return dict(checked) if (checked := _checked[storage.url, prefix, fingerprints]) is not None else None


def _check(storage: Remote, prefix: str, trusted: Signer | None, fingerprints: frozenset[str]) -> dict[str, str] | None:
    """Return the files of the MANIFEST of `prefix`, when signed with the keys of `fingerprints`.

    Only the text gpg verified is read, not what may be around its signed part.
    """
    with tempfile.TemporaryDirectory(prefix="xcraft-manifest-") as tmp:
        work = Path(tmp)
        manifest, payload = work / MANIFEST, work / "payload"
        if not download(storage, f"{prefix}/{MANIFEST}", manifest):
            return None
        keyring = work / "keys"
        keyring.mkdir(mode=0o700)
        verifier = Signer(key="", gnupghome=keyring)
        keys = [k for fingerprint in storage.build_keys if (k := _build_key(storage, fingerprint))]
        (work / "keys.asc").write_text("\n".join([*keys, *([trusted.public_key()] if trusted else [])]))
        try:
            if keys or trusted:
                verifier.gpg("--import", str(work / "keys.asc"))
            status = verifier.gpg("--status-fd", "1", "--output", str(payload), "--decrypt", str(manifest))
            files = _parse(prefix, payload.read_text())
        except (SigningError, ValueError):
            log.warning("ignoring invalid remote tree %s", prefix)
            return None
    valid = any(
        line.startswith("[GNUPG:] VALIDSIG ") and (line.split()[2] in fingerprints or line.split()[-1] in fingerprints)
        for line in status.splitlines()
    )
    return files if valid else None


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


def update_tree(
    project: Project,
    prefix: str,
    dest: Path,
    wanted: Callable[[str, str], bool],
    on_line: Callable[[str], None] = lambda _: None,
) -> bool:
    """Download into the directory `dest`, MANY at once, the files of the trusted tree `prefix`, from main or dev, that
    `wanted` takes, by path and SHA-256, and return whether a storage has it. Each file is checked against the signed
    manifest, then renamed into `dest`: those missing, corrupted or failing to download are skipped.
    """  # noqa: D205
    error, found = None, None
    for storage in storages(project):
        try:
            if (manifest := _manifest(storage, prefix, dev_signer(project))) is not None:
                found = storage, manifest
                break
        except RemoteError as e:
            error = e
    if found is None:
        if error:
            raise error
        return False
    storage, manifest = found
    dest.mkdir(parents=True, exist_ok=True)
    # next to it, to rename them there
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=f".{dest.name}-") as tmp:

        def fetch(name: str) -> None:
            part = Path(tmp) / name
            try:
                found = download(storage, f"{prefix}/{name}", part, on_line)
            except RemoteError as e:  # one of many, which the others need not wait for
                log.warning("skipping %s/%s: %s", prefix, name, e)
                return
            if found and sha256_of(part) == manifest[name]:
                (dest / name).parent.mkdir(parents=True, exist_ok=True)
                part.rename(dest / name)

        with ThreadPoolExecutor(MANY) as pool:
            list(pool.map(fetch, [name for name, digest in manifest.items() if wanted(name, digest)]))
    return True


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

        def fetch(name: str, digest: str) -> None:
            if not download(storage, f"{prefix}/{name}", tree / name, on_line) or sha256_of(tree / name) != digest:
                msg = f"{prefix}/{name} is missing or corrupted in the storage"
                raise RemoteError(msg)

        with ThreadPoolExecutor(REQUESTS) as pool:
            list(pool.map(fetch, manifest, manifest.values()))
        try:
            tree.rename(dest)
        except OSError:
            if not dest.is_dir():  # but made meanwhile by another command, from the same tree
                raise
    return True


def required(signer: Signer | None) -> Signer:
    """Return `signer`, which the uploads need to sign the manifests of their trees."""
    if signer is None:
        msg = "no key to sign uploads: run xcraft key create, configure [sign], or use --key"
        raise RemoteError(msg)
    return signer


def upload_tree(storage: Remote, prefix: str, src: Path, names: list[str], *, signer: Signer | None) -> dict[str, str]:
    """Upload the entries `names` of the directory `src` as the tree `prefix`, and return its files' SHA-256."""
    required(signer)
    files = sorted(f for name in names for f in [src / name, *(src / name).rglob("*")] if f.is_file())
    manifest = {f.relative_to(src).as_posix(): sha256_of(f) for f in files}
    transfer(storage, {f"{prefix}/{name}": src / name for name in manifest})
    write_manifest(storage, prefix, manifest, signer=signer)
    return manifest


def read_manifest(storage: Remote, prefix: str, *, trusted: Signer | None = None) -> dict[str, str]:
    """Return the SHA-256 of the files of the tree `prefix`, by path in it, or nothing without its manifest."""
    return _manifest(storage, prefix, trusted) or {}


def write_manifest(storage: Remote, prefix: str, manifest: dict[str, str], *, signer: Signer | None) -> None:
    """Write the manifest of the tree `prefix`, which completes it."""
    signer = required(signer)
    TreeManifest(files=manifest)
    with tempfile.TemporaryDirectory(prefix="xcraft-sign-") as tmp:
        work = Path(tmp)
        payload, file = work / "payload", work / MANIFEST
        payload.write_text(manifest_text(prefix, manifest))
        signer.clearsign(payload, file)
        key = work / "key.asc"
        key.write_text(signer.public_key())
        upload(storage, f"{KEYS}/{signer.key}.asc", key)
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
table { border-collapse: collapse; table-layout: fixed; width: 100% }
th { font-size: .7rem; font-weight: 600; text-transform: uppercase; letter-spacing: .08em;
     color: #8b8b94; cursor: pointer; user-select: none }
td { border-top: 1px solid #ececf1; color: #6d6d76 }
th, td { padding: .4rem 0; text-align: left }
th + th, td + td { padding-left: 1.5rem }
th:nth-child(n+2), td:nth-child(n+2) { text-align: right; white-space: nowrap }
th:nth-child(2) { width: 6rem }
th:nth-child(3) { width: 9rem }
td:first-child { color: inherit; overflow-wrap: anywhere }
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
        metadata = {
            key.lower(): value for key, value in s3.head_object(Bucket=bucket, Key=prefix + INDEX)["Metadata"].items()
        }
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


CCACHE = f"{INTERNAL}/ccache"
"""The compiler caches, a tree by architecture and package, see xcraft.ccache."""


def _paged(directory: str) -> bool:
    """Return whether `directory` has a page: not those in the trees of the compiler caches, too many for their use."""
    return not directory.startswith(f"{CCACHE}/") or directory.count("/") <= CCACHE.count("/") + 3


def update_pages(project: Project, storage: Remote, prefixes: list[str]) -> int:
    """Write the pages of the directories under the directories `prefixes`, and of their parents; return how many.

    The directories are the prefixes ending with ``/``, and ``""`` is the root. Those without page, see _paged(), count
    in the size of the directory above.
    """
    pages: dict[str, tuple[_Dirs, dict[str, _Entry]]] = {}
    parents: set[str] = set()
    with _writing(storage, "make the directory pages") as s3:
        if _objects(s3, storage.bucket, f"{KEYS}/", below=True)[1]:
            prefixes = [*prefixes, f"{KEYS}/"]
        # those under another one are in its listing
        for prefix in sorted(p for p in set(prefixes) if not any(p != q and p.startswith(q) for q in prefixes)):
            pages.setdefault(prefix, ({}, {}))
            for path, (size, date) in _objects(s3, storage.bucket, prefix, below=True)[1].items():
                *parts, name = path.split("/")
                for i, part in enumerate(parts):
                    if not _paged(directory := prefix + "".join(f"{p}/" for p in parts[:i])):
                        break
                    dirs = pages.setdefault(directory, ({}, {}))[0]
                    below, newest = dirs.get(part, (0, None))
                    dirs[part] = (below + size, max(filter(None, (newest, date))))
                if _paged(directory := prefix + "".join(f"{p}/" for p in parts)):
                    pages.setdefault(directory, ({}, {}))[1][name] = (size, date)
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

        with ThreadPoolExecutor(REQUESTS) as pool:
            list(pool.map(write, pages))
    return len(pages)
