"""The remote storage, with a Garage server, which serves the public reads with its website endpoint."""

from __future__ import annotations

import getpass
import http.client
import http.server
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests import ARCH, PLATFORM, TOML, copy_project
from tests.test_build import results, write_spec, xcraft
from tests.test_iso import cache_image
from xcraft import buildid, ccache, remote, repodata
from xcraft import publish as publisher
from xcraft.container import run
from xcraft.iso import build_iso, iso_key, iso_name, iso_path, push_isos
from xcraft.lock import load_lock
from xcraft.project import Project
from xcraft.resolution import resolve_image
from xcraft.rpmbuild import prepare_tree
from xcraft.signing import Signer, check_signatures, create_dev_key, dev_signer, release_signer
from xcraft.sources import SourceError, cache_source, sha256_of, source_path, write_manifest

if TYPE_CHECKING:
    from collections.abc import Iterator


GARAGE = "docker.io/dxflrs/garage:v2.1.0"
# a garage for each worker of pytest-xdist, with its name and ports
WORKER = int(os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw"))
CONTAINER = f"xcraft-test-garage-{WORKER}"
S3_PORT, WEB_PORT = 13900 + 10 * WORKER, 13902 + 10 * WORKER
WEB = f"http://xcraft.web.garage.localhost:{WEB_PORT}"
STAGE = getpass.getuser()


def garage(*args: str) -> str:
    return subprocess.run(
        ["podman", "exec", CONTAINER, "/garage", *args], check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture(scope="module")
def s3(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """Start a Garage server with a bucket, a key to write it, and public reads; return its settings."""
    garage_config = tmp_path_factory.mktemp("garage") / "garage.toml"
    garage_config.write_text(
        'metadata_dir = "/var/lib/garage/meta"\ndata_dir = "/var/lib/garage/data"\ndb_engine = "sqlite"\n'
        f'replication_factor = 1\nrpc_bind_addr = "[::]:3901"\nrpc_secret = "{secrets.token_hex(32)}"\n'
        '[s3_api]\ns3_region = "garage"\napi_bind_addr = "[::]:3900"\nroot_domain = ".s3.garage.localhost"\n'
        '[s3_web]\nbind_addr = "[::]:3902"\nroot_domain = ".web.garage.localhost"\nindex = "index.html"\n'
    )
    subprocess.run(["podman", "rm", "-f", CONTAINER], check=True, capture_output=True)
    subprocess.run(
        ["podman", "run", "-d", "--rm", "--name", CONTAINER, "-p", f"127.0.0.1:{S3_PORT}:3900",
         "-p", f"127.0.0.1:{WEB_PORT}:3902", "-v", f"{garage_config}:/etc/garage.toml:ro,Z", GARAGE],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        for _ in range(50):
            try:
                node = garage("node", "id", "-q").split("@")[0].strip()
                break
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        else:
            pytest.fail("the Garage server did not start")
        garage("layout", "assign", "-z", "dc1", "-c", "1G", node)
        garage("layout", "apply", "--version", "1")
        garage("bucket", "create", "xcraft")
        key = garage("key", "create", "test")
        garage("bucket", "allow", "--read", "--write", "--owner", "xcraft", "--key", "test")
        garage("bucket", "website", "--allow", "xcraft")
        garage("bucket", "create", "xcraft-dev")
        garage("bucket", "allow", "--read", "--write", "xcraft-dev", "--key", "test")
        garage("bucket", "website", "--allow", "xcraft-dev")
        fields = dict(re.findall(r"^(Key ID|Secret key):\s+(\S+)$", key, re.MULTILINE))
        yield {"AWS_ACCESS_KEY_ID": fields["Key ID"], "AWS_SECRET_ACCESS_KEY": fields["Secret key"]}
    finally:
        subprocess.run(["podman", "rm", "-f", CONTAINER], check=True, capture_output=True)


def page(directory: str) -> str:
    """Return the page of a directory of the storage, through its public endpoint."""
    with urllib.request.urlopen(f"{WEB}/{directory}") as response:  # noqa: S310, the test garage
        assert response.headers["Content-Type"].startswith("text/html")
        assert response.headers["Cache-Control"] == "no-cache"
        return response.read().decode()


REMOTE = (
    # only the host's: publish fetches the builds of the others
    TOML.replace('platforms = ["linux/amd64/v2", "linux/arm64"]', f'platforms = ["{PLATFORM}"]')
    + f'[remote.main]\nurl = "{WEB}"\ns3_endpoint = "http://localhost:{S3_PORT}"\nbucket = "xcraft"\n'
    + f'[remote.dev]\nurl = "http://xcraft-dev.web.garage.localhost:{WEB_PORT}"\n'
    + f's3_endpoint = "http://localhost:{S3_PORT}"\nbucket = "xcraft-dev"\n'
    # not the build repository, with the hundreds of source packages of their build environment to download
    '[publish]\nrelease = "9.0"\nbuild = false\n'
)


@pytest.fixture(scope="module")
def build_signer(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    home = tmp_path_factory.mktemp("build-key")
    home.chmod(0o700)
    signer = Signer("", home)
    signer.gpg("--passphrase", "", "--quick-generate-key", "CI test", "rsa2048", "sign", "never")
    fingerprint = next(
        line.split(":")[9]
        for line in signer.gpg("--with-colons", "--list-secret-keys").splitlines()
        if line.startswith("fpr:")
    )
    return fingerprint, home


@pytest.fixture
def root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    s3: dict[str, str],
    build_environment: None,
    build_signer: tuple[str, Path],
) -> Path:
    root = tmp_path / "project"
    copy_project(root)
    monkeypatch.setattr(
        "tests.test_remote.REMOTE",
        REMOTE.replace(
            "[remote.main]",
            f'[sign]\nkey = "{build_signer[0]}"\ngnupghome = "{build_signer[1]}"\n'
            f'[remote.main]\nbuild_keys = ["{build_signer[0]}"]',
        ),
    )
    (root / "xcraft.toml").write_text(REMOTE)
    monkeypatch.setenv("GNUPGHOME", str(tmp_path / "unused-keyring"))
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    for name, value in s3.items():
        monkeypatch.setenv(name, value)
    return root


def test_iso_cache(root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    project = Project.find()
    signer = release_signer(project)
    assert signer
    image = tmp_path / "test.iso"
    image.write_bytes(secrets.token_bytes(2048))
    tree = cache_image(project, image, signer=signer)
    code, output = xcraft("push", "isos")
    assert code == 0, output
    assert "1 ISOs pushed" in output
    assert "0 ISOs pushed, 1 already" in xcraft("push", "isos")[1]
    storage = project.config.remote["dev"]
    snapshot = tmp_path / "snapshot"
    published = snapshot / "iso" / iso_name(project, "8.99.0", netinstall=False, signer=signer)
    published.parent.mkdir(parents=True)
    shutil.copy2(image, published)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        stored = push_isos(project, storage, signer=signer)
        publisher.upload_isos(project, snapshot, storage, "cache-reuse", signer=signer, stored=stored)
    assert any(m.startswith("copying ") and "/image.iso to " in m for m in caplog.messages)
    assert not any(m.startswith(f"uploading {published} ") for m in caplog.messages)
    assert not any(m.startswith("downloading ") and f"/{published.name} to " in m for m in caplog.messages)
    # with the checksums of the ISOs of all the architectures, signed
    manifest = remote.read_manifest(storage, f"isos/9.0/{STAGE}/cache-reuse", trusted=signer)
    assert {published.name, publisher.SUMS, f"{publisher.SUMS}.asc"} <= manifest.keys()
    sums = tmp_path / publisher.SUMS
    assert remote.download(storage, f"isos/9.0/{STAGE}/cache-reuse/{publisher.SUMS}", sums)
    assert sums.read_text() == f"{sha256_of(image)}  {published.name}\n"
    shutil.rmtree(project.state / "cache")
    # No snapshot, RPMs, prepared container state or signing operations are needed to fetch the image.
    project.frozen = True
    assert build_iso(project, signer=signer).read_bytes() == image.read_bytes()
    assert tree.exists()
    assert not (project.state / "publish").exists()
    assert not (project.state / "cache/builds").exists()
    shutil.rmtree(tree)
    key = iso_key(project, netinstall=False, volid=None, signer=signer)
    remote.upload(project.config.remote["dev"], f"{iso_path(project, key)}/image.iso", root / "xcraft.toml")
    with pytest.raises(remote.RemoteError, match="missing or corrupted"):
        # The transport rejects the changed payload before installing its cache directory.
        remote.fetch_tree(project, iso_path(project, key), tree)
    assert not tree.exists()


def test_sources(root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    archive = tmp_path / f"trivial-{secrets.token_hex(4)}.tar.gz"
    archive.write_text(archive.name)
    cache = Project.find().source_cache
    digest = cache_source(cache, archive)
    write_manifest(root / "base/trivial/sources", {archive.name: digest})

    code, output = xcraft("push", "sources", "--check", "--to", "main")
    assert code == 1
    assert f"✗ {archive.name} is not in the remote storage" in output
    with caplog.at_level(logging.DEBUG, logger="xcraft"):  # what xcraft --debug shows
        assert xcraft("push", "sources")[1].endswith("1 pushed, 0 already in the remote storage\n")
    assert (
        f"uploading {cache / digest / archive.name} to s3://xcraft-dev/.xcraft/sources/{digest}/{archive.name}"
        in caplog.messages
    )
    assert xcraft("push", "sources", "--check", "--to", "main")[0] == 1
    shutil.rmtree(cache)
    assert (prepare_tree(Project.find(), "trivial") / "SOURCES" / archive.name).read_text() == archive.name
    assert xcraft("push", "--to", "main")[0] == 1  # sources are uploaded before reporting the missing builds
    assert xcraft("push", "sources", "--check", "--to", "main") == (0, "1 archives in the remote storage, 0 missing\n")

    # a fresh checkout downloads it
    shutil.rmtree(cache)
    assert (prepare_tree(Project.find(), "trivial") / "SOURCES" / archive.name).read_text() == archive.name
    shutil.rmtree(cache)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        assert xcraft("fetch", "sources") == (0, "all the source archives are in the source cache\n")
    assert any(m.startswith(f"downloading {WEB}/.xcraft/sources/{digest}/") for m in caplog.messages)
    assert (cache / digest / archive.name).read_text() == archive.name
    # and checks it
    shutil.rmtree(cache)
    storage = Project.find().config.remote.get("main")
    assert storage
    remote.upload(storage, source_path(archive.name, digest), root / "xcraft.toml")
    with pytest.raises(SourceError, match="does not match the sha256 of the manifest"):
        prepare_tree(Project.find(), "trivial")


def test_sources_from_the_spec(root: Path, tmp_path: Path) -> None:
    archive, extra = (tmp_path / f"{name}-{secrets.token_hex(4)}.tar.gz" for name in ("trivial", "extra"))
    for path in (archive, extra):
        path.write_text(path.name)
    write_manifest(root / "base/trivial/sources", {path.name: sha256_of(path) for path in (archive, extra)})
    spec = root / "base/trivial/trivial.spec"
    sources = f"Source0: file://{archive}\nSource1: file://{extra}"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", f"BuildArch: noarch\n{sources}"))
    assert xcraft("fetch", "sources", "trivial") == (0, "all the source archives are in the source cache\n")
    for path in (archive, extra):  # Source1's too
        assert (Project.find().source_cache / sha256_of(path) / path.name).read_text() == path.name
    shutil.rmtree(Project.find().source_cache)
    code, output = xcraft("push", "--to", "main")
    assert code == 1
    assert "not in the source cache" in output
    assert xcraft("fetch", "sources", "trivial")[0] == 0
    code, output = xcraft("push", "--to", "main")
    assert code == 1, output
    assert "2 pushed, 0 already in the remote storage" in output
    assert xcraft("push", "sources", "trivial", "--check", "--to", "main") == (
        0,
        "2 archives in the remote storage, 0 missing\n",
    )
    write_manifest(root / "base/trivial/sources", {archive.name: sha256_of(archive)})
    # checked too
    write_manifest(root / "base/trivial/sources", {archive.name: "0" * 64})
    code, output = xcraft("fetch", "sources", "trivial")
    assert code == 1
    assert f"{archive.name} from file://{archive} does not match the sha256 of the manifest" in output
    # a download failing, for the package only
    spec.write_text(
        spec.read_text().replace(f"Source0: file://{archive}", f"Source0: file://{tmp_path}/gone/{archive.name}")
    )
    code, output = xcraft("fetch", "sources")
    assert code == 1
    assert f"✗ trivial: {archive.name} is not in the source cache, and cannot be downloaded: " in output
    # found nowhere, without a directory left in the cache
    spec.write_text(
        spec.read_text().replace(f"Source0: file://{tmp_path}/gone/{archive.name}", "Source0: other.tar.gz")
    )
    code, output = xcraft("fetch")
    assert code == 1
    assert output.endswith(
        f"✗ trivial: {archive.name} found nowhere: not in the remote storage, nor at a URL of the spec\n"
    )
    assert not (Project.find().source_cache / ("0" * 64)).exists()


def test_signed_trees(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote.get("main")
    assert storage
    src = tmp_path / "tree"
    src.mkdir()
    (src / "file").write_text("trusted")
    prefix = f"builds/security/{secrets.token_hex(4)}"
    signer = release_signer(Project.find())
    assert signer
    with pytest.raises(remote.RemoteError, match="no key to sign uploads"):
        remote.upload_tree(storage, prefix, src, ["file"], signer=None)
    # Verification keys neither select the signer nor need to be configured for uploads.
    remote.upload_tree(storage.model_copy(update={"build_keys": []}), prefix, src, ["file"], signer=signer)
    assert remote.download_tree(storage, prefix, tmp_path / "valid")
    assert (tmp_path / "valid/file").read_text() == "trusted"
    assert remote.tree_exists(storage.model_copy(update={"build_keys": ["0" * 40, *storage.build_keys]}), prefix)
    assert not remote.tree_exists(storage.model_copy(update={"build_keys": []}), prefix)
    key = tmp_path / "key.asc"
    key.write_text(signer.public_key())
    remote.upload(storage, f"{remote.KEYS}/{'0' * 40}.asc", key)
    assert not remote.tree_exists(storage.model_copy(update={"build_keys": ["0" * 40]}), prefix)
    # A signed manifest cannot be transplanted to another build key.
    remote.transfer(storage, {f"{prefix}-other/{f}": f"{prefix}/{f}" for f in ("file", remote.MANIFEST)})
    assert not remote.download_tree(storage, f"{prefix}-other", tmp_path / "wrong-path")
    # Changed bytes fail even when the manifest is authentic.
    (src / "file").write_text("tampered")
    remote.upload(storage, f"{prefix}/file", src / "file")
    with pytest.raises(remote.RemoteError, match="corrupted"):
        remote.download_tree(storage, prefix, tmp_path / "corrupt")
    assert not (tmp_path / "corrupt").exists()
    # The manifest is signed in clear: the path of the tree, then its files like sha256sum's.
    signed = urllib.request.urlopen(f"{storage.url}/{prefix}/{remote.MANIFEST}").read().decode()  # noqa: S310
    text = f"{prefix}\n{sha256_of(tmp_path / 'valid/file')}  file\n"
    assert signed.startswith("-----BEGIN PGP SIGNED MESSAGE-----\nHash: ")
    assert text in signed
    # Only its signed part is read: lines around it do not add files.
    tampered = f"{sha256_of(src / 'file')}  file"
    (src / remote.MANIFEST).write_text(f"{signed}{tampered}\n")
    remote.upload(storage, f"{prefix}/{remote.MANIFEST}", src / remote.MANIFEST)
    assert remote.read_manifest(storage, prefix) == {"file": sha256_of(tmp_path / "valid/file")}
    # Replacing both bytes and manifest still cannot forge the signature, nor remove it.
    for forged in (signed.replace(text, f"{prefix}\n{tampered}\n"), f"{prefix}\n{tampered}\n"):
        (src / remote.MANIFEST).write_text(forged)
        remote.upload(storage, f"{prefix}/{remote.MANIFEST}", src / remote.MANIFEST)
        assert not remote.tree_exists(storage, prefix)


@pytest.mark.parametrize("kind", ["builds", "ccache"])
def test_tree_fallback_checks_signatures(
    root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    project = Project.find()
    main, dev = project.config.remote["main"], project.config.remote["dev"]
    signer = release_signer(project)
    assert signer
    src = tmp_path / "src"
    src.mkdir()
    (src / "file").write_text("trusted payload")
    prefix = f"{kind}/security/{secrets.token_hex(4)}"
    manifest = remote.upload_tree(dev, prefix, src, ["file"], signer=signer)
    # Main has an unsigned tree. Only its metadata may be downloaded before trying dev.
    remote.upload(main, f"{prefix}/file", src / "file")
    (src / remote.MANIFEST).write_text(remote.manifest_text(prefix, manifest))
    remote.upload(main, f"{prefix}/{remote.MANIFEST}", src / remote.MANIFEST)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        assert remote.tree_available(project, prefix)
        assert remote.fetch_tree(project, prefix, tmp_path / "valid")
    assert (tmp_path / "valid/file").read_text() == "trusted payload"
    assert not any(f"downloading {main.url}/{prefix}/file " in line for line in caplog.messages)
    assert any(f"downloading {dev.url}/{prefix}/file " in line for line in caplog.messages)
    # The bucket does not grant trust. An unknown signer must not trigger payload downloads in either bucket.
    project.config.remote["main"] = main.model_copy(update={"build_keys": ["0" * 40]})
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        assert not remote.tree_available(project, prefix)
        assert not remote.fetch_tree(project, prefix, tmp_path / "untrusted")
    assert not any(f"/{prefix}/file " in line for line in caplog.messages)
    assert not (tmp_path / "untrusted").exists()
    # A signed but corrupt main payload can fall back to a valid dev copy too.
    project.config.remote["main"] = main
    remote.upload_tree(main, prefix, src, ["file"], signer=signer)
    (src / "file").write_text("corrupted main payload")
    remote.upload(main, f"{prefix}/file", src / "file")
    assert remote.fetch_tree(project, prefix, tmp_path / "fallback")
    assert (tmp_path / "fallback/file").read_text() == "trusted payload"


def test_developer_permissions(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    storage = Project.find().config.remote.get("main")
    assert storage
    dev_storage = Project.find().config.remote["dev"]
    key_name = f"developer-{secrets.token_hex(4)}"
    fields = dict(re.findall(r"^(Key ID|Secret key):\s+(\S+)$", garage("key", "create", key_name), re.MULTILINE))
    garage("bucket", "allow", "--read", "--write", "xcraft-dev", "--key", key_name)
    archive = tmp_path / f"source-{secrets.token_hex(4)}"
    archive.write_text("source archive")
    digest = sha256_of(archive)
    path = source_path(archive.name, digest)
    write_manifest(root / "base/trivial/sources", {archive.name: digest})
    with monkeypatch.context() as developer:
        developer.setenv("AWS_ACCESS_KEY_ID", fields["Key ID"])
        developer.setenv("AWS_SECRET_ACCESS_KEY", fields["Secret key"])
        remote.upload(dev_storage, path, archive)
        with pytest.raises(remote.RemoteError):
            remote.upload(storage, path, archive)
        with pytest.raises(remote.RemoteError):
            remote.delete(storage, [path])
    code, output = xcraft("push", "--to", "main")
    assert code == 1
    assert "not in the source cache" in output
    assert xcraft("fetch", "sources")[0] == 0
    code, output = xcraft("push", "--to", "main")
    assert code == 1, output
    assert f"↑ {archive.name}" in output
    assert remote.download(storage, path, tmp_path / "uploaded")
    assert sha256_of(tmp_path / "uploaded") == digest
    archive.write_text("corrupted developer archive")
    remote.upload(dev_storage, path, archive)
    shutil.rmtree(Project.find().source_cache)
    assert xcraft("push", "--to", "main")[0] == 1  # a changed developer copy cannot replace an existing main copy
    assert remote.download(storage, path, tmp_path / "still-trusted")
    assert sha256_of(tmp_path / "still-trusted") == digest
    remote.delete(storage, [path])
    code, output = xcraft("push", "--to", "main")
    assert code == 1
    assert "not in the source cache" in output
    code, output = xcraft("fetch", "sources")
    assert code == 1
    assert "does not match the sha256 of the manifest" in output
    assert not remote.exists(storage, path)


def test_developer_storage(root: Path, tmp_path: Path) -> None:
    config = re.sub(r"\[sign\]\n.*?(?=\[remote.main\])", "", REMOTE, flags=re.DOTALL)
    config = re.sub(r"\[remote.main\]\n.*?(?=\[remote.dev\])", "", config, flags=re.DOTALL)
    (root / "xcraft.toml").write_text(config)
    signer = create_dev_key(Project.find(), "Developer")
    archive = tmp_path / f"source-{secrets.token_hex(4)}"
    archive.write_text("developer source")
    digest = cache_source(Project.find().source_cache, archive)
    write_manifest(root / "base/trivial/sources", {archive.name: digest})
    assert xcraft("push", "sources")[0] == 0
    shutil.rmtree(Project.find().source_cache)
    assert xcraft("fetch")[0] == 0  # a developer-only configuration can fetch sources without a main bucket
    assert (Project.find().source_cache / digest / archive.name).read_text() == archive.read_text()
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    assert xcraft("build", "trivial", "--fetch=none")[0] == 0
    assert xcraft("push", "builds", "trivial")[0] == 0
    storage = Project.find().config.remote["dev"]
    trees = remote.listing(storage, f".xcraft/builds/{ARCH}/trivial/")
    assert any(path.endswith(remote.MANIFEST) for path in trees)
    assert not any(path.endswith(".asc") for path in trees if not path.endswith(remote.MANIFEST))
    assert signer.key not in storage.build_keys
    shutil.rmtree(root / ".xcraft/cache/builds")
    code, output = xcraft("fetch", "builds", "trivial")
    assert code == 0, output
    assert "1 fetched" in output
    # Compiler caches also fall back to dev when main cannot be reached.
    (root / "xcraft.toml").write_text(config + '\n[remote.main]\nurl = "http://localhost:1"\n')
    cache = root / ".xcraft/ccache/trivial"
    (cache / "0").mkdir(parents=True, exist_ok=True)  # like those of ccache
    (cache / "0/entry").write_text("cached object")
    assert xcraft("push", "ccache", "trivial")[0] == 0
    shutil.rmtree(cache)
    code, output = xcraft("fetch", "ccache", "trivial")
    assert code == 0, output
    assert (cache / "0/entry").read_text() == "cached object"
    (root / "xcraft.toml").write_text(re.sub(r"\[remote.dev\]\n.*?(?=\[publish\])", "", config, flags=re.DOTALL))
    code, output = xcraft("push", "sources")
    assert code == 2
    assert "remote.dev" in output


def test_builds_shared_by_checkouts(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_signer: tuple[str, Path]
) -> None:
    # CI selects its key without a private-key setting in the project.
    (root / "xcraft.toml").write_text(re.sub(r"\[sign\]\n.*?(?=\[remote.main\])", "", REMOTE, flags=re.DOTALL))
    monkeypatch.setenv("GNUPGHOME", str(build_signer[1]))
    # a key of its own, not to find the builds of the other tests in the storage
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    assert xcraft("build", "trivial")[0] == 0
    assert xcraft("push", "builds", "trivial", "--key", build_signer[0], "--to", "main")[0] == 0
    assert xcraft("push", "--key", build_signer[0], "--to", "main", "builds", "trivial")[0] == 0
    # another checkout, elsewhere, with what is committed: the same keys, whatever the path
    other = tmp_path / "elsewhere" / "xcraft"
    shutil.copytree(root, other, ignore=shutil.ignore_patterns(".xcraft"))
    monkeypatch.setenv("XCRAFT_PROJECT", str(other))
    assert Project.find().id != Project(root, Project.find().config).id
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert output.splitlines()[0] == "↓ trivial fetched"
    assert output.splitlines()[-1] == "1 built, 1 fetched"


def test_push_everything(root: Path, tmp_path: Path) -> None:
    # keys and an archive of its own, not to find those of the other tests in the storage
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    archive = tmp_path / f"trivial-{secrets.token_hex(4)}.tar.gz"
    archive.write_text(archive.name)
    write_manifest(root / "base/trivial/sources", {archive.name: cache_source(Project.find().source_cache, archive)})
    # and one of a package not built, which the source cache has not, like those of the builds that failed, with the
    # same name as the one of trivial, which is pushed still
    write_manifest(root / "base/with-deps/sources", {archive.name: "0" * 64})
    assert xcraft("build", "trivial")[0] == 0
    code, output = xcraft("push", "--to", "main")
    # all that can be pushed, and then what could not
    assert code == 1
    assert f"↑ {archive.name}\n1 pushed, 0 already in the remote storage\n↑ trivial\n1 pushed, 0 already" in output
    # the pages of what was pushed only: xcraft push index makes them all
    assert output.endswith(
        f"1 pushed, 0 already in the remote storage\nnot in the source cache: {archive.name}; not built: with-deps"
    )
    assert f'href="{archive.name}"' in page(f".xcraft/sources/{sha256_of(archive)}/")
    assert xcraft("push", "sources", "trivial", "--check", "--to", "main")[0] == 0


def test_builds(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    assert (
        xcraft("push", "builds", "trivial", "--to", "main")[1]
        == "↑ trivial\n1 pushed, 0 already in the remote storage\n"
    )
    # browsable, as the storage does not list directories
    assert 'href=".xcraft/"' in page("")
    assert 'href="builds/"' in page(".xcraft/")
    assert 'href="trivial/"' in page(f".xcraft/builds/{ARCH}/")
    # with the size of all that is below them and the date of the newest file, from their pages when only their
    # parent is listed
    assert re.search(
        r'href="builds/">builds/</a></td><td data-k="[1-9]\d*"[^>]*>[\d.]+ \w+</td><td data-k="\d{4}-', page(".xcraft/")
    )
    assert xcraft("push", "index", "--to", "main")[1].endswith(" pages\n")
    assert xcraft("push", "builds", "--to", "main") == (
        1,
        "0 pushed, 1 already in the remote storage\nnot built: with-deps",
    )

    # a fresh checkout downloads it, with xcraft fetch, or instead of building it
    shutil.rmtree(root / ".xcraft/cache/builds")
    assert xcraft("fetch", "builds") == (0, "↓ trivial\n1 fetched, 0 already cached, 1 not in the remote storage\n")
    shutil.rmtree(root / ".xcraft/cache/builds")
    assert xcraft("build", "with-deps", "--dry-run")[1].splitlines()[:2] == ["fetch    trivial", "build    with-deps"]
    assert xcraft("build", "with-deps", "--dry-run", "--fetch=none")[1].splitlines()[:2] == [
        "build    trivial",
        "build    with-deps",
    ]
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert output.splitlines()[0] == "↓ trivial fetched"
    assert output.splitlines()[-1] == "1 built, 1 fetched"
    assert list(
        (root / f".xcraft/cache/builds/{ARCH}/trivial").glob("*/RPMS/noarch/trivial-1.0-1+b1.xcpng8.99.noarch.rpm")
    )
    # built, even though the storage has it
    shutil.rmtree(root / f".xcraft/cache/builds/{ARCH}/trivial")
    code, output = xcraft("build", "trivial", "--fetch=none")
    assert code == 0, output
    assert output.splitlines()[-1] == "1 built"


def test_fetch_needed(root: Path) -> None:
    # trivial and other in the storage, with-deps, built against trivial, not yet
    lock = (root / "xcraft.lock").read_text()
    write_spec(root, "other")
    assert xcraft("build", "trivial", "other")[0] == 0
    assert xcraft("push", "builds", "trivial", "other", "--to", "main")[0] == 0
    shutil.rmtree(root / ".xcraft/cache/builds")
    # without what rpm generated for trivial in the lock: fetched for with-deps, it changes the key of with-deps
    (root / "xcraft.lock").write_text(lock)
    builds = root / f".xcraft/cache/builds/{ARCH}"
    # only trivial is fetched, for the build of with-deps
    code, output = xcraft("build", "--dry-run", "--fetch=needed")
    assert code == 0, output
    assert output.splitlines() == [
        "fetch    trivial",
        "build    with-deps",
        "1 to build, 1 to fetch, 1 in the remote storage",
    ]
    code, output = xcraft("build", "--fetch=needed")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == [
        "↓ trivial fetched",
        "✓ with-deps built",
        "1 built, 1 fetched, 1 stored",
    ]
    assert sorted(p.name for p in builds.iterdir()) == ["trivial", "with-deps"]
    # the build in the storage only is there already
    assert xcraft("push", "builds", "--to", "main") == (0, "↑ with-deps\n1 pushed, 2 already in the remote storage\n")
    # nothing to fetch, with all in the storage
    shutil.rmtree(root / ".xcraft/cache/builds")
    assert xcraft("build", "--fetch=needed") == (0, "3 stored\n")
    assert not builds.exists() or not list(builds.iterdir())


def ccache_hits(cache: Path) -> int:
    """Return how many compilations were found in the compiler cache `cache`."""
    run(Project.find(), "test-ccache", "CCACHE_DIR=/cache ccache --print-stats", volumes=[f"{cache}:/cache"])
    log = (Project.find().state / "logs/_test-ccache.log").read_text()
    stats = {k: int(v) for k, v in (line.split("\t") for line in log.splitlines() if "\t" in line)}
    return stats["direct_cache_hit"] + stats["preprocessed_cache_hit"]


@pytest.mark.parametrize("developer", [False, True])
def test_ccache(  # noqa: PLR0915
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_signer: tuple[str, Path],
    caplog: pytest.LogCaptureFixture,
    *,
    developer: bool,
) -> None:
    config = re.sub(r"\[sign\]\n.*?(?=\[remote.main\])", "", REMOTE, flags=re.DOTALL)
    if developer:
        config = re.sub(r"build_keys = \[.*?\]", "build_keys = []", config)
    (root / "xcraft.toml").write_text(config)
    if developer:
        create_dev_key(Project.find(), "Developer <dev@example.com>")
    else:
        monkeypatch.setenv("GNUPGHOME", str(build_signer[1]))
    write_spec(root, "compiled", files="/usr/lib/compiled/t.o")
    spec = root / "base/compiled/compiled.spec"
    build = "%build\necho 'int f(void) { return 0; }' > t.c\ngcc -c t.c -o t.o\n"
    spec.write_text(
        spec.read_text()
        .replace("BuildArch: noarch\n", "")
        .replace("%install\n", build + "%install\n")
        .replace("touch %{buildroot}/usr/lib/compiled/t.o", "cp t.o %{buildroot}/usr/lib/compiled/t.o")
    )
    assert xcraft("build", "compiled")[0] == 0
    cache = root / ".xcraft/ccache/compiled"
    results = sorted(cache.rglob("*R"))  # the results of the compilations
    assert results
    options = [] if developer else ["--key", build_signer[0]]

    def levels() -> set[int]:
        return {path.read_bytes()[5] for path in cache.rglob("*R")}  # in the header of ccache's entries

    assert levels() == {1}  # ccache's, for the builds

    def push() -> list[str]:
        """Push the cache, and return the entries uploaded."""
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="xcraft"):
            assert xcraft("push", "ccache", "compiled", "--to", "main", *options) == (0, "↑ compiled\n1 pushed\n")
        return sorted(m.split(f"/{tree}/")[1] for m in caplog.messages if m.startswith("uploading ") and tree in m)

    tree = f".xcraft/ccache/{ARCH}/compiled"
    storage = Project.find().config.remote["main"]
    entries = [p.relative_to(cache).as_posix() for p in results]
    dates = {p: p.stat().st_mtime for p in cache.rglob("*") if p.is_file()}
    assert set(entries) <= set(push())
    assert [m for m in caplog.messages if "--recompress" in m]
    # compressed again in place, once, with the dates which tell the entries of the last build
    assert levels() == {ccache.LEVEL}
    assert {p: p.stat().st_mtime for p in dates} == dates
    # an object by entry, in a signed tree, with a page, but not its directories, too many
    stored = remote.read_manifest(storage, tree, trusted=dev_signer(Project.find()))
    assert set(entries) <= stored.keys()
    assert sorted(remote.listing(storage, f"{tree}/")) == sorted([*stored, remote.MANIFEST])
    assert 'href="manifest.asc"' in page(f"{tree}/")
    assert 'href="compiled/"' in page(f".xcraft/ccache/{ARCH}/")
    with pytest.raises(urllib.error.HTTPError):
        page(f"{tree}/{entries[0].split('/')[0]}/")
    # only what the storage does not have is uploaded, and compressed: nothing
    assert push() == []
    assert not [m for m in caplog.messages if "--recompress" in m]
    shutil.rmtree(cache)
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert sorted(cache.rglob("*R")) == results
    assert levels() == {ccache.LEVEL}  # compressed again, for smaller transfers
    # a fetch adds what the local cache misses to it
    (results[0]).unlink()
    (cache / "local").write_text("kept")
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert sorted(cache.rglob("*R")) == results
    assert (cache / "local").read_text() == "kept"
    (cache / "local").unlink()
    # a build without a compiler cache gets the one of the remote storage, and uses it, unless --fetch=none
    for release, options, hits in (("2", ["--fetch=none"], 0), ("3", [], 1)):
        shutil.rmtree(cache)
        spec.write_text(re.sub(r"Release: \d+", f"Release: {release}", spec.read_text()))
        assert xcraft("build", "compiled", *options)[0] == 0
        assert ccache_hits(cache) == hits


def test_ccache_of_the_last_build(root: Path, tmp_path: Path) -> None:
    write_spec(root, "compiled", files="/usr/lib/compiled/t.o")
    spec = root / "base/compiled/compiled.spec"
    template = (
        spec.read_text()
        .replace("BuildArch: noarch\n", "")
        .replace("touch %{buildroot}/usr/lib/compiled/t.o", "cp t.o %{buildroot}/usr/lib/compiled/t.o")
    )
    cache = root / ".xcraft/ccache/compiled"

    def build(release: str, value: str) -> None:
        compile_ = f"%build\necho 'int f(void) {{ return {value}; }}' > t.c\ngcc -c t.c -o t.o\n"
        spec.write_text(
            template.replace("Release: 1", f"Release: {release}").replace("%install\n", compile_ + "%install\n")
        )
        assert xcraft("build", "compiled", "--fetch=none")[0] == 0

    build("1", "1")
    build("2", "2")
    first, second = sorted(cache.rglob("*R"), key=lambda path: path.stat().st_mtime)
    # only a hit on the second: its entries only are pushed, with their last use
    build("3", "2")
    assert ccache_hits(cache) == 1
    assert xcraft("push", "ccache", "compiled", "--to", "main") == (0, "↑ compiled\n1 pushed\n")
    shutil.rmtree(cache)
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert sorted(cache.rglob("*R")) == [second]  # not first, of the first build
    assert first != second
    # the next push deletes what the last build did not use, and what a push interrupted before its manifest left
    storage = Project.find().config.remote["main"]
    tree = f".xcraft/ccache/{ARCH}/compiled"
    remote.upload(storage, f"{tree}/orphan", spec)
    build("4", "4")
    assert xcraft("push", "ccache", "compiled", "--to", "main") == (0, "↑ compiled\n1 pushed\n")
    stored = remote.read_manifest(storage, tree, trusted=dev_signer(Project.find()))
    assert second.relative_to(cache).as_posix() not in stored
    assert sorted(remote.listing(storage, f"{tree}/")) == sorted([*stored, remote.MANIFEST])
    # the archive of a cache of an older xcraft is not an entry
    (tmp_path / "compiled.ccache").write_text("archive")
    remote.upload_tree(storage, tree, tmp_path, ["compiled.ccache"], signer=release_signer(Project.find()))
    shutil.rmtree(cache)
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert not (cache / "compiled.ccache").exists()


def test_unavailable_remote_storage(root: Path) -> None:
    (root / "xcraft.toml").write_text(REMOTE.replace(str(WEB_PORT), "13899"))  # of no worker
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert output.splitlines()[1].startswith(  # after the build IDs
        "remote storage unavailable, building locally: http://xcraft-dev.web.garage.localhost:13899/"
    )
    assert output.splitlines()[-1] == "1 built"


def test_push_needs_the_credentials(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    # not the build the other tests pushed: the same key everywhere
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "no credentials"'))
    assert xcraft("build", "trivial")[0] == 0
    code, output = xcraft("push", "builds", "trivial", "--to", "main")
    assert code == 1
    assert "pushing needs the S3 credentials" in output


def test_publish(root: Path) -> None:
    # the architectures the host runs, built for all: two on x86_64
    arches = [ARCH, "x86_64"] if ARCH == "x86_64_v2" else [ARCH]
    platforms = ", ".join(f'"{p}"' for p in [PLATFORM, "linux/amd64"][: len(arches)])
    (root / "xcraft.toml").write_text(REMOTE.replace(f'["{PLATFORM}"]', f"[{platforms}]") + 'extra = ["bc"]\n')
    code, output = xcraft("publish", "--to", "main", "--unsigned")
    assert code == 0, output
    assert {"bc", "glibc"} <= set(load_lock(Project.find()).runtime[ARCH])
    bc = resolve_image(Project.find()).packages["bc"]
    assert bc.repo == "baseos"
    storage = Project.find().config.remote.get("main")
    assert storage
    day = datetime.now(UTC).date().isoformat()
    for name in (day, "latest"):
        base = f"repos/9.0/{STAGE}/{name}/base"
        for repo in (*arches, "Source"):
            assert remote.exists(storage, f"{base}/{repo}/repodata/repomd.xml")
            assert remote.exists(
                storage, f"{base}/{repo}/trivial-1.0-1+b1.xcpng8.99.{'src' if repo == 'Source' else 'noarch'}.rpm"
            )
        # the sources of the upstream packages too
        assert remote.exists(storage, f"{base}/Source/bc-{bc.evr}.src.rpm")
        assert 'href="trivial-1.0-1%2Bb1.xcpng8.99.noarch.rpm"' in page(f"{base}/{arches[-1]}/")
    # a second one the same day, without changing the locks, of the repositories only, like all without [iso]
    code, output = xcraft("publish", "repos", "--to", "main", "--locked", "--unsigned")
    assert code == 0, output
    assert f"published repos/9.0/{STAGE}/{day}.2, and as repos/9.0/{STAGE}/latest" in output
    assert not remote.tree_exists(storage, f"isos/9.0/{STAGE}/latest")
    # An explicit stage has an independent snapshot sequence and latest tree.
    code, output = xcraft("publish", "--to", "main", "--stage", "incoming", "repos", "--locked", "--unsigned")
    assert code == 0, output
    assert f"published repos/9.0/incoming/{day}, and as repos/9.0/incoming/latest" in output
    assert remote.tree_exists(storage, f"repos/9.0/{STAGE}/latest")
    assert remote.tree_exists(storage, "repos/9.0/incoming/latest")
    code, output = xcraft("publish", "isos", "--to", "main", "--unsigned")
    assert code == 2
    assert "no ISO to publish: see [iso] in xcraft.toml" in output
    # a local one is of the host's architecture, unless asked
    code, output = xcraft("publish", "--local")
    assert code == 0, output
    assert sorted(p.name for p in (root / ".xcraft/publish/base").iterdir()) == ["Source", ARCH]
    if len(arches) > 1:
        assert f"warning: only {ARCH} is published, not x86_64: --arch all publishes them all" in output
        code, output = xcraft("publish", "--local", "--arch", "x86_64")
        assert code == 0, output
        assert "warning" not in output
        assert sorted(p.name for p in (root / ".xcraft/publish/base").iterdir()) == ["Source", "x86_64"]
    # what our packages need at runtime is checked
    (root / "xcraft.toml").write_text(REMOTE)
    assert xcraft("build", "--dry-run")[0] == 0  # the lock of the architecture left
    buildid.update(Project.find())  # and the build IDs, which cover all the architectures
    # the options of publish apply to its subcommands too, given before them
    for args in (["--locked", "--local"], ["--locked", "--local", "repos"], ["--locked", "repos", "--local"]):
        code, output = xcraft("publish", *args)
        assert code == 1
        assert re.search(rf"--locked forbids: .*-runtime\.{ARCH} bc\b", output)
    # the upstream packages must be signed with the keys of the repositories
    (root / "xcraft.toml").write_text(f'{REMOTE}extra = ["bc"]\n')
    srpm = root / f".xcraft/cache/srpms/bc-{bc.evr}.src.rpm"
    shutil.copy(next(root.glob(f".xcraft/cache/builds/{ARCH}/trivial/*/SRPMS/*.src.rpm")), srpm)
    code, output = xcraft("publish", "--local")
    assert code == 1
    assert f"✗ base/Source/{srpm.name}: digests SIGNATURES NOT OK" in output
    assert "some upstream packages are not signed with the keys of the repositories" in output


@pytest.mark.skipif(not shutil.which("gpg"), reason="needs gpg")
def test_signed_publish(root: Path) -> None:  # noqa: PLR0915
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    unsigned = re.sub(r"\[sign\]\n.*?(?=\[remote.main\])", "", REMOTE, flags=re.DOTALL)
    (root / "xcraft.toml").write_text(unsigned)
    code, output = xcraft("publish", "--to", "main")
    assert code == 2
    assert "no key to sign the packages" in " ".join(output.replace("│", "").split())
    signer = create_dev_key(Project.find(), "Release <release@example.com>")
    try:
        # the developer's key, without a release one
        code, output = xcraft("publish", "--local")
        assert code == 0, output
        assert (root / ".xcraft/publish/RPM-GPG-KEY").read_text() == signer.public_key()
        # Its remote builds and snapshots are trusted without adding the developer key to the config.
        code, output = xcraft("push", "builds", "--to", "main")
        assert code == 0, output
        shutil.rmtree(root / ".xcraft/cache/builds")
        code, output = xcraft("fetch", "builds")
        assert code == 0, output
        assert "fetched" in output
        code, output = xcraft("publish", "--to", "main")
        assert code == 0, output
        storage = Project.find().config.remote.get("main")
        assert storage
        assert remote.tree_exists(storage, f"repos/9.0/{STAGE}/latest", trusted=signer)
        assert not remote.tree_exists(storage, f"repos/9.0/{STAGE}/latest")
        assert xcraft("publish", "--to", "main")[0] == 0
        (root / "xcraft.toml").write_text(
            re.sub(r"build_keys = \[.*?\]", f'build_keys = ["{signer.key}"]', unsigned)
            + f'[sign]\nkey = "{signer.key}"\ngnupghome = "{signer.gnupghome}"\n'
        )
        code, output = xcraft("publish", "--to", "main")
        assert code == 0, output
        snapshot = root / ".xcraft/publish"
        assert (snapshot / "RPM-GPG-KEY").read_text() == signer.public_key()
        for repo in (ARCH, "Source"):
            assert (snapshot / "base" / repo / "repodata/repomd.xml.asc").exists()
        ours = [f"base/{ARCH}/trivial-1.0-1+b1.xcpng8.99.noarch.rpm", "base/Source/trivial-1.0-1+b1.xcpng8.99.src.rpm"]
        check_signatures(Project.find(), snapshot, ours, what="packages of ours", public_key=signer.public_key())
        storage = Project.find().config.remote.get("main")
        assert storage
        assert remote.exists(storage, f"repos/9.0/{STAGE}/latest/base/{ARCH}/repodata/repomd.xml.asc")
        signed = list((root / ".xcraft/cache/signed" / signer.key / ARCH / "trivial").iterdir())
        assert remote.tree_exists(storage, f".xcraft/signed/{signer.key}/{ARCH}/trivial/{signed[0].name}")
        # signed once: the next snapshots have the same files
        rpm = signed[0] / "RPMS/noarch/trivial-1.0-1+b1.xcpng8.99.noarch.rpm"
        before = rpm.read_bytes()
        assert xcraft("publish", "--to", "main")[0] == 0
        assert rpm.read_bytes() == before
        config = root / "xcraft.toml"
        config.write_text(config.read_text().replace(f'key = "{signer.key}"', f'key = "{"0" * 40}"'))
        code, output = xcraft("publish", "--key", signer.key, "--to", "main", "repos", "--locked")
        assert code == 0, output
        # Developer publishing defaults to the independent dev bucket and ignores the configured release key.
        code, output = xcraft("publish", "repos", "--locked")
        assert code == 0, output
        dev_storage = Project.find().config.remote["dev"]
        assert remote.tree_exists(dev_storage, f"repos/9.0/{STAGE}/latest", trusted=signer)
        assert not remote.tree_exists(dev_storage, f"repos/9.0/{STAGE}/latest")
        assert remote.tree_exists(
            dev_storage, f".xcraft/signed/{signer.key}/{ARCH}/trivial/{signed[0].name}", trusted=signer
        )
    finally:
        subprocess.run(["gpgconf", "--homedir", str(signer.gnupghome), "--kill", "gpg-agent"], check=False)


def test_publish_rebuilt_package(root: Path, tmp_path: Path) -> None:
    """A rebuild keeping its version-release is published, not the previous build of the storage."""
    code, output = xcraft("publish", "--to", "main", "--unsigned")
    assert code == 0, output
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello, rebuilt"'))
    code, output = xcraft("publish", "--to", "main", "--unsigned")
    assert code == 0, output
    storage = Project.find().config.remote.get("main")
    assert storage
    rpm = f"base/{ARCH}/trivial-1.0-1+b1.xcpng8.99.noarch.rpm"
    local = root / ".xcraft/publish" / rpm
    for name in (re.search(r"published (repos/\S+),", output)[1], f"repos/9.0/{STAGE}/latest"):  # ty: ignore[not-subscriptable]
        assert remote.read_manifest(storage, name)[rpm] == sha256_of(local)
        assert remote.download(storage, f"{name}/{rpm}", tmp_path / name.replace("/", "_"))
        assert sha256_of(tmp_path / name.replace("/", "_")) == sha256_of(local)


def test_publish_from_latest_metadata(root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A snapshot has the metadata of the packages of the latest one, without downloading them."""
    stage = f"metadata-{secrets.token_hex(4)}"
    (root / "xcraft.toml").write_text(f'{REMOTE}extra = ["bc"]\n')
    code, output = xcraft("publish", "repos", "--to", "main", "--unsigned", "--stage", stage)
    assert code == 0, output
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello, rebuilt"'))
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        code, output = xcraft("publish", "repos", "--to", "main", "--unsigned", "--stage", stage)
    assert code == 0, output
    assert not [m for m in caplog.messages if "downloading " in m and ".rpm to " in m]
    # each manifest checked once
    manifests = [
        m.split()[1] for m in caplog.messages if m.startswith("downloading ") and f"/{remote.MANIFEST} to " in m
    ]
    assert len(manifests) == len(set(manifests))
    # the unchanged packages are already in latest
    latest = f"s3://xcraft/repos/9.0/{stage}/latest/"
    copied = [m.split(" to ")[-1] for m in caplog.messages if m.startswith("copying ") and f" to {latest}" in m]
    assert copied
    assert all(Path(f).name.startswith(("trivial-", "with-deps-")) for f in copied if f.endswith(".rpm"))
    # only those rebuilt, trivial and the package built against it, not the upstream ones
    local = {p.name for p in root.glob(".xcraft/publish/**/*.rpm")}
    assert local
    assert all(name.startswith(("trivial-", "with-deps-")) for name in local)
    storage = Project.find().config.remote["main"]
    latest = f"repos/9.0/{stage}/latest"
    manifest = remote.read_manifest(storage, latest)
    bc = resolve_image(Project.find()).packages["bc"]
    assert {f"base/{ARCH}/bc-{bc.evr}.{bc.arch}.rpm", f"base/Source/bc-{bc.evr}.src.rpm"} <= manifest.keys()
    for repo in (f"base/{ARCH}", "base/Source"):
        assert remote.download(storage, f"{latest}/{repo}/repodata/repomd.xml", tmp_path / repo / "repodata/repomd.xml")
        for href in repodata.files((tmp_path / repo / "repodata/repomd.xml").read_bytes()).values():
            assert remote.download(storage, f"{latest}/{repo}/{href}", tmp_path / repo / href)
        entries = repodata.read(tmp_path / repo)
        assert entries
        assert {f"{repo}/{repodata.location(e)}": pkgid for pkgid, e in entries.items()} == {
            f: digest for f, digest in manifest.items() if f.startswith(f"{repo}/") and f.endswith(".rpm")
        }
    # the packages of a repository, for its ISO
    rpms = {f: (f"{latest}/{f}", digest) for f, digest in manifest.items() if f.endswith(".rpm")}
    publisher.materialize(storage, tmp_path / "packages", rpms, f"base/{ARCH}")
    assert {p.relative_to(tmp_path / "packages").as_posix() for p in (tmp_path / "packages").rglob("*.rpm")} == {
        f for f in rpms if f.startswith(f"base/{ARCH}/")
    }


def test_publish_reuses_remote_snapshot(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    stage = f"reuse-{secrets.token_hex(4)}"
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage)
    assert code == 0, output
    project = Project.find()
    project.config.publish.stage = stage
    for name in ("publish", "cache/builds", "cache/signed", "cache/rpms", "cache/srpms", "cache/checks"):
        shutil.rmtree(project.state / name, ignore_errors=True)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        code, output = xcraft("publish", "repos", "--to", "main", "--stage", stage, "--locked")
    assert code == 0, output
    assert "reused the matching snapshot" in output
    assert not (project.state / "publish").exists()
    assert not (project.state / "cache/builds").exists()
    assert not any(
        "podman " in message or ("downloading " in message and ".rpm to " in message) for message in caplog.messages
    )
    storage = project.config.remote["main"]
    prefix = f"repos/9.0/{stage}"
    day = datetime.now(UTC).date().isoformat()
    manifest = remote.read_manifest(storage, f"{prefix}/{day}.2")
    assert manifest
    assert manifest == remote.read_manifest(storage, f"{prefix}/latest")
    # what it was made from is out of it
    assert not [f for f in manifest if "inputs" in f]
    assert remote.read_manifest(storage, publisher.inputs_tree(f"{prefix}/latest")).keys() == {publisher.INPUTS}

    project.config.publish.extra.append("bc")
    assert not publisher.reuse_published(
        [project], storage, signer=release_signer(project), build=False, only="repos", isos=[]
    )
    project.config.publish.extra.clear()
    # nor another latest snapshot than the one it was made from
    signer = release_signer(project)
    assert signer
    remote.write_manifest(storage, f"{prefix}/latest", {**manifest, "other": "0" * 64}, signer=signer)
    assert not publisher.reuse_published([project], storage, signer=signer, build=False, only="repos", isos=[])
    remote.write_manifest(storage, f"{prefix}/latest", manifest, signer=signer)
    assert publisher.reuse_published([project], storage, signer=signer, build=False, only="repos", isos=[])
    # nor with what it was made from changed
    remote.upload(storage, f"{publisher.inputs_tree(f'{prefix}/latest')}/{publisher.INPUTS}", root / "xcraft.toml")
    assert not publisher.reuse_published([project], storage, signer=signer, build=False, only="repos", isos=[])


@pytest.mark.parametrize("command", [[], ["repos"], ["isos"]])
@pytest.mark.parametrize("stage", ["", ".", "..", "../other", "/absolute", "a/b"])
def test_invalid_publish_stage(root: Path, command: list[str], stage: str) -> None:
    code, output = xcraft("publish", *command, "--local", "--stage", stage)
    assert code == 2
    assert "stage must start" in " ".join(output.split())


def test_iso_stages(root: Path, tmp_path: Path) -> None:
    project = Project.find()
    storage = project.config.remote["main"]
    signer = release_signer(project)
    assert signer
    isos = tmp_path / "iso" / ARCH
    isos.mkdir(parents=True)
    image = isos / "install.iso"
    day = datetime.now(UTC).date().isoformat()
    for stage in ("incoming", "testing"):
        project.config.publish.stage = stage
        assert publisher.snapshot_name(project, storage) == day
        image.write_text(stage)
        publisher.upload_isos(project, tmp_path, storage, day, signer=signer)
        assert publisher.snapshot_name(project, storage) == f"{day}.2"
    for stage in ("incoming", "testing"):
        for name in (day, "latest"):
            prefix = f"isos/9.0/{stage}/{name}"
            assert remote.download_tree(storage, prefix, tmp_path / stage / name)
            assert (tmp_path / stage / name / ARCH / "install.iso").read_text() == stage


def test_iso_snapshot_names_with_an_unconfigured_signer(root: Path) -> None:
    project = Project.find()
    project.config.publish.stage = f"snapshot-name-{secrets.token_hex(4)}"
    storage = project.config.remote["dev"]
    signer = release_signer(project)
    assert signer
    assert not storage.build_keys
    day = datetime.now(UTC).date().isoformat()
    prefix = f"isos/9.0/{project.config.publish.stage}/{day}"
    remote.write_manifest(storage, prefix, {}, signer=signer)
    assert not remote.tree_exists(storage, prefix)
    assert publisher.snapshot_name(project, storage) == f"{day}.2"


def test_manifest_paths_stay_in_their_tree(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote.get("main")
    assert storage
    signer = release_signer(Project.find())
    assert signer
    key = tmp_path / "key.asc"
    key.write_text(signer.public_key())
    remote.upload(storage, f"{remote.KEYS}/{signer.key}.asc", key)
    payload = tmp_path / "payload"
    # nor be changed by the signature in clear, which drops the spaces ending its lines
    with pytest.raises(ValueError, match="invalid tree path"):
        remote.write_manifest(storage, "trees/bad", {"escaped ": "0" * 64}, signer=signer)
    for name in ("../escaped", "/var/escaped", "a/../../escaped"):
        payload.write_text(f"trees/bad\n{'0' * 64}  {name}\n")
        signer.clearsign(payload, tmp_path / remote.MANIFEST)
        remote.upload(storage, f"trees/bad/{remote.MANIFEST}", tmp_path / remote.MANIFEST)
        assert not remote.download_tree(storage, "trees/bad", tmp_path / "tree")
    assert not list(tmp_path.glob("**/escaped"))


def test_pages_of_nested_directories(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote["main"]
    (tmp_path / "file").write_bytes(b"x" * 1000)
    remote.upload(storage, "nested/a/b/file", tmp_path / "file")
    remote.update_pages(Project.find(), storage, ["nested/a/", "nested/"])
    assert 'href="b/">b/</a></td><td data-k="1000"' in page("nested/a/")
    assert 'href="a/">a/</a></td><td data-k="1000"' in page("nested/")
    assert "<img" not in page("nested/")


def test_directory_totals_behind_proxy(root: Path, tmp_path: Path) -> None:
    project = Project.find()
    storage = project.config.remote["main"]
    file = tmp_path / "file"
    file.write_bytes(b"x" * 1000)
    remote.upload(storage, "cased/file", file)
    remote.update_pages(project, storage, ["cased/"])

    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_HEAD(self) -> None:
            with closing(http.client.HTTPConnection("localhost", S3_PORT)) as connection:
                connection.request("HEAD", self.path, headers=dict(self.headers.items()))
                response = connection.getresponse()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    self.send_header(key.title(), value)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    with http.server.ThreadingHTTPServer(("localhost", 0), Proxy) as proxy:
        thread = threading.Thread(target=proxy.serve_forever)
        thread.start()
        try:
            proxied = storage.model_copy(update={"s3_endpoint": f"http://localhost:{proxy.server_port}"})
            with remote._writing(proxied, "read cached totals") as s3:  # noqa: SLF001
                size, newest = remote._stored_total(s3, storage.bucket, "cased/")  # noqa: SLF001
            assert size == 1000
            assert newest
        finally:
            proxy.shutdown()
            thread.join()


def test_signing_keys_have_a_directory_page(root: Path, tmp_path: Path) -> None:
    project = Project.find()
    storage = project.config.remote["main"]
    signer = release_signer(project)
    assert signer
    file = tmp_path / "file"
    file.write_text("signed tree")
    remote.upload_tree(storage, "pages-with-key", tmp_path, ["file"], signer=signer)
    remote.update_pages(project, storage, ["pages-with-key/"])
    assert f'href="{signer.key}.asc"' in page(".xcraft/keys/")
    internal = page(".xcraft/")
    assert 'href="keys/"' in internal
    assert re.search(r'href="keys/".*?<td data-k="[1-9][0-9]*">', internal)


def test_logo_on_the_pages(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote["main"]
    (tmp_path / "file").write_bytes(b"x" * 1000)
    remote.upload(storage, "nested/x", tmp_path / "file")
    remote.update_pages(Project.find(), storage, ["nested/"])
    assert "<img" not in page("nested/")
    before, sep, after = (root / "xcraft.toml").read_text().partition("\n[")
    (root / "xcraft.toml").write_text(f'{before}logo = "https://xcp-ng.org/assets/img/mainlogo.png"\n{sep}{after}')
    remote.update_pages(Project.find(), Project.find().config.remote["main"], ["nested/"])
    assert '<img src="https://xcp-ng.org/assets/img/mainlogo.png" alt="">' in page("nested/")


def test_development_builds_are_neither_pushed_nor_fetched(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args], cwd=root, check=True,
                       capture_output=True)  # fmt: skip

    git("init", "-q", "-b", "master")
    buildid.update(Project.find())
    git("add", "-A")
    git("commit", "-qm", "base")
    git("checkout", "-qb", "feature")
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    assert xcraft("build", "trivial")[0] == 0
    code, output = xcraft("push", "builds", "--to", "main")
    assert code == 0, output
    assert output.splitlines()[0] == "development builds, not pushed: trivial, with-deps"
    assert "0 pushed" in output
    code, output = xcraft("fetch", "builds")
    assert code == 0, output
    assert output.splitlines()[0] == "development builds, not fetched: trivial, with-deps"
    # their final builds, explicitly
    assert xcraft("build", "trivial", "--final")[0] == 0
    code, output = xcraft("push", "builds", "trivial", "--final", "--to", "main")
    assert code == 0, output
    assert "↑ trivial" in output


def test_reused_builds_are_fetched(root: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@e", *args], cwd=root, check=True,
                       capture_output=True)  # fmt: skip

    # keys of their own, not to find the builds of the other tests in the storage
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    assert xcraft("build")[0] == 0
    git("init", "-q", "-b", "master")
    buildid.update(Project.find())
    git("add", "-A")
    git("commit", "-qm", "base")
    assert xcraft("push", "builds", "--to", "main")[0] == 0
    # on a branch, with-deps reuses the build of the main branch, built against the previous trivial
    git("checkout", "-qb", "feature")
    trivial = root / "base/trivial/trivial.spec"
    trivial.write_text(f"# changed\n{trivial.read_text()}")
    assert xcraft("reuse", "with-deps")[0] == 0
    shutil.rmtree(root / ".xcraft/cache/builds")
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert "↓ with-deps fetched" in output
    assert output.splitlines()[-1] == "1 built, 1 fetched"
    # never built again
    shutil.rmtree(root / ".xcraft/cache/builds")
    code, output = xcraft("build", "with-deps", "--fetch=none")
    assert code == 1
    assert "✗ with-deps: not in the remote storage" in output
