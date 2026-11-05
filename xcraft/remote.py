"""The shared storage: public reads over http(s), authenticated writes with S3.

It has the published repositories and ISOs, in ``repos/`` and ``isos/``, and,
in ``.xcraft/``, what xcraft keeps for itself: the source archives, as
``.xcraft/sources/<sha256>/<filename>`` like the local source cache, and the
builds, as ``.xcraft/builds/<arch>/<package>/<key>/`` trees, with the signed
builds, the compiler caches, the resolutions, the public keys, the ISOs of the checkouts, and the
copy of the pinned upstream packages, see xcraft.upstream_copy.
Garage, like other S3 storages, stores identical files once: copies cost
nothing, so builds are stored as their files, for the published repositories
to be copies of them.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import email.message
import functools
import hashlib
import os
import re
import tempfile
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING
from urllib.parse import urlparse

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


def _client(storage: Remote) -> S3Client:
    """Return the S3 client of `storage`, with the credentials of the environment."""
    if not (storage.s3_endpoint and storage.bucket):
        msg = "pushing needs the s3_endpoint and bucket of the storage, in xcraft.toml"
        raise RemoteError(msg)
    if not (os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY")):
        msg = "pushing needs the S3 credentials, in AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY"
        raise RemoteError(msg)
    return _s3(
        storage.s3_endpoint,
        storage.region,
        os.environ["AWS_ACCESS_KEY_ID"],
        os.environ["AWS_SECRET_ACCESS_KEY"],
        os.environ.get("AWS_SESSION_TOKEN", ""),
    )


@contextlib.contextmanager
def writing(storage: Remote, action: str) -> Generator[S3Client]:
    """Return the S3 client of `storage`, reporting the S3 errors of `action` as RemoteError."""
    client = _client(storage)
    try:
        yield client
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
    """
    _forget(storage, list(files))
    with writing(storage, f"write {len(files)} files") as s3:

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
    """
    with writing(storage, f"list {prefix}") as s3:
        return list(objects(s3, storage.bucket, prefix, below=True)[1])


def delete(storage: Remote, paths: list[str]) -> None:
    """Delete `paths` from the storage."""
    for path in paths:
        log.debug("deleting %s", _s3_url(storage, path))
    _forget(storage, paths)
    with writing(storage, f"delete {len(paths)} files") as s3:
        for i in range(0, len(paths), 1000):  # the most S3 deletes at once
            deleted = s3.delete_objects(
                Bucket=storage.bucket, Delete={"Objects": [{"Key": p} for p in paths[i : i + 1000]]}
            )
            # those it could not delete, in a response of success, but those it did not have, like Garage reports them
            if errors := [e for e in deleted.get("Errors", []) if e.get("Code") != "NoSuchKey"]:
                msg = f"cannot delete {', '.join(f'{e.get("Key")}: {e.get("Message")}' for e in errors)}"
                raise RemoteError(msg)


KEYS = f"{INTERNAL}/keys"
"""The public keys of the signed trees, by fingerprint, in the storage."""
MANIFEST = "manifest.asc"
"""The files of a tree in the storage, with their SHA-256, signed in clear, uploaded last: a tree without it is
incomplete. Its text is the path of its files, then a ``<sha256>  <file>`` line by file, like sha256sum's, sorted.

The files of the trees made once, like the builds, are in a directory of the tree of their own, its generation, named
by the SHA-256 of what they are, see upload_tree(): uploads of other files, like those of the same build made on two
machines at once, never write over them, and whichever manifest is written last, its files are as it says. Those of
the trees updated in place, like the snapshots and the compiler caches, and those of older xcraft versions, are in
the tree itself."""


class TreeManifest(BaseModel):
    """The files of a tree of the remote storage, with their SHA-256."""

    files: dict[str, str]

    @field_validator("files")
    @classmethod
    def valid_files(cls, files: dict[str, str]) -> dict[str, str]:
        """Refuse the paths out of the tree, and what are not SHA-256."""
        for name, digest in files.items():
            if not _in_tree(name):
                msg = f"invalid tree path: {name}"
                raise ValueError(msg)
            if not re.fullmatch("[0-9a-f]{64}", digest):
                msg = f"invalid SHA-256: {name}"
                raise ValueError(msg)
        return files


def _in_tree(name: str) -> bool:
    """Return whether `name` is a path in a tree, written as it is, and not changed by the signature in clear, which
    ignores the spaces ending the lines.
    """
    path = PurePosixPath(name)
    inside = path.parts and not path.is_absolute() and ".." not in path.parts
    return bool(inside) and path.as_posix() == name == name.strip() and name.isprintable()


def manifest_text(root: str, files: dict[str, str]) -> str:
    """Return the text of a MANIFEST of `files`, in `root`, which it signs."""
    return "".join([f"{root}\n", *(f"{files[name]}  {name}\n" for name in sorted(files))])


def generation(prefix: str, files: dict[str, str]) -> str:
    """Return the directory of the files `files` of the tree `prefix`, made once: see MANIFEST."""
    return f"{prefix}/{hashlib.sha256(manifest_text(prefix, files).encode()).hexdigest()}"


def _parse(prefix: str, text: str) -> tuple[str, dict[str, str]]:
    """Return the directory of the files of the text of the MANIFEST of `prefix`, and those files, or raise
    ValueError.
    """
    root, *lines = text.removesuffix("\n").split("\n")
    if (
        not re.fullmatch(rf"{re.escape(prefix)}(/[0-9a-f]{{64}})?", root)
        or not text.endswith("\n")
        or any(line[64:66] != "  " for line in lines)
    ):
        msg = f"not the manifest of {prefix}"
        raise ValueError(msg)
    if len(files := {line[66:]: line[:64] for line in lines}) != len(lines):
        msg = f"files listed twice in the manifest of {prefix}"
        raise ValueError(msg)
    if root != prefix and root != generation(prefix, files):
        msg = f"not the generation of the files of the manifest of {prefix}"
        raise ValueError(msg)
    return root, TreeManifest(files=files).files


_checked: dict[tuple[str, str, frozenset[str]], tuple[str, dict[str, str]] | None] = {}
"""The manifests checked, by storage, tree and trusted fingerprints, with the directory of their files, or None for
the trees missing or not trusted, until written, or the next command."""
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
    trees = {path.rsplit("/", 1)[0] for path in paths if path.endswith(f"/{MANIFEST}")}
    for key in [k for k in _checked if k[0] == storage.url and k[1] in trees]:
        del _checked[key]


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


def _manifest(storage: Remote, prefix: str, trusted: Signer | None) -> tuple[str, dict[str, str]] | None:
    fingerprints = frozenset({*storage.build_keys, *([trusted.key.upper()] if trusted else [])})
    if not fingerprints:
        return None
    if (storage.url, prefix, fingerprints) not in _checked:
        _checked[storage.url, prefix, fingerprints] = _check(storage, prefix, trusted, fingerprints)
    return (checked[0], dict(checked[1])) if (checked := _checked[storage.url, prefix, fingerprints]) else None


def _check(
    storage: Remote, prefix: str, trusted: Signer | None, fingerprints: frozenset[str]
) -> tuple[str, dict[str, str]] | None:
    """Return the directory of the files of the MANIFEST of `prefix`, and those files, when signed with the keys of
    `fingerprints`.

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
            parsed = _parse(prefix, payload.read_text())
        except (SigningError, ValueError):
            log.warning("ignoring invalid remote tree %s", prefix)
            return None
    valid = any(
        line.startswith("[GNUPG:] VALIDSIG ") and (line.split()[2] in fingerprints or line.split()[-1] in fingerprints)
        for line in status.splitlines()
    )
    return parsed if valid else None


def tree_exists(storage: Remote, prefix: str, *, trusted: Signer | None = None) -> bool:
    """Return whether the storage has the complete tree `prefix`."""
    return _manifest(storage, prefix, trusted) is not None


def storages(project: Project) -> list[Remote]:
    """Return the storages, main then dev, each trusting the keys of both: a tree is as trusted in either."""
    keys = sorted({key for storage in project.config.remote.values() for key in storage.build_keys})
    return [
        storage.model_copy(update={"build_keys": keys})
        for name in ("main", "dev")
        if (storage := project.config.remote.get(name))
    ]


def _first[T](project: Project, attempt: Callable[[Remote], T]) -> T | None:
    """Return the first result of `attempt` on main, then dev, that is not empty, or else raise the error of one, if
    any.
    """
    error = None
    for storage in storages(project):
        try:
            if result := attempt(storage):
                return result
        except RemoteError as e:
            error = e
    if error:
        raise error
    return None


def tree_available(project: Project, prefix: str) -> bool:
    """Return whether main, or else dev, has the trusted tree `prefix`, from its manifest only."""
    return bool(_first(project, lambda s: tree_exists(s, prefix, trusted=dev_signer(project))))


def fetch_tree(project: Project, prefix: str, dest: Path, on_line: Callable[[str], None] = lambda _: None) -> bool:
    """Download the trusted tree `prefix` from main, or else dev, as `dest`, its manifest checked before its files,
    and return whether one has it.
    """
    return bool(_first(project, lambda s: download_tree(s, prefix, dest, on_line, trusted=dev_signer(project))))


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
    """
    found = _first(project, lambda s: (s, m) if (m := _manifest(s, prefix, dev_signer(project))) is not None else None)
    if found is None:
        return False
    storage, (root, manifest) = found
    dest.mkdir(parents=True, exist_ok=True)
    # next to it, to rename them there
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=f".{dest.name}-") as tmp:

        def fetch(name: str) -> None:
            part = Path(tmp) / name
            try:
                found = download(storage, f"{root}/{name}", part, on_line)
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
        if (found := _manifest(storage, prefix, trusted)) is None:
            return False
        root, manifest = found
        tree.mkdir()

        def fetch(name: str, digest: str) -> None:
            if not download(storage, f"{root}/{name}", tree / name, on_line) or sha256_of(tree / name) != digest:
                msg = f"{root}/{name} is missing or corrupted in the storage"
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
    """Upload the entries `names` of the directory `src` as the tree `prefix`, made once, in its generation of their
    files, and return their SHA-256: see MANIFEST.
    """
    required(signer)
    files = sorted(f for name in names for f in [src / name, *(src / name).rglob("*")] if f.is_file())
    manifest = {f.relative_to(src).as_posix(): sha256_of(f) for f in files}
    root = generation(prefix, manifest)
    transfer(storage, {f"{root}/{name}": src / name for name in manifest})
    write_manifest(storage, prefix, manifest, signer=signer, root=root)
    return manifest


def read_tree(storage: Remote, prefix: str, *, trusted: Signer | None = None) -> tuple[str, dict[str, str]]:
    """Return the directory of the files of the tree `prefix` in the storage, and their SHA-256, by path in the tree:
    none without its manifest.
    """
    return _manifest(storage, prefix, trusted) or (prefix, {})


def read_manifest(storage: Remote, prefix: str, *, trusted: Signer | None = None) -> dict[str, str]:
    """Return the SHA-256 of the files of the tree `prefix`, by path in it, or nothing without its manifest."""
    return read_tree(storage, prefix, trusted=trusted)[1]


def write_manifest(
    storage: Remote, prefix: str, manifest: dict[str, str], *, signer: Signer | None, root: str | None = None
) -> None:
    """Write the manifest of the tree `prefix`, of its files in `root`, its generation, or else in the tree itself,
    which completes it.
    """
    signer = required(signer)
    TreeManifest(files=manifest)
    with tempfile.TemporaryDirectory(prefix="xcraft-sign-") as tmp:
        work = Path(tmp)
        payload, file = work / "payload", work / MANIFEST
        payload.write_text(manifest_text(root or prefix, manifest))
        signer.clearsign(payload, file)
        key = work / "key.asc"
        key.write_text(signer.public_key())
        upload(storage, f"{KEYS}/{signer.key}.asc", key)
        upload(storage, f"{prefix}/{MANIFEST}", file)


INDEX = "index.html"
"""The page of a directory, which the storage serves for it, but does not make."""

Entry = tuple[int, dt.datetime]
"""Size and date of a file."""


def objects(s3: S3Client, bucket: str, prefix: str, *, below: bool) -> tuple[set[str], dict[str, Entry]]:
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


CCACHE = f"{INTERNAL}/ccache"
"""The compiler caches, a tree by architecture and package, see xcraft.ccache."""
RESOLVED = f"{INTERNAL}/resolved"
"""The resolutions, a tree by architecture and key, see xcraft.resolution."""
