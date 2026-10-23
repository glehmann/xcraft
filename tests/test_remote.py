"""The remote storage, with a Garage server, which serves the public reads with its website endpoint."""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests import ARCH, PLATFORM, PROJECT, TOML
from tests.test_build import write_spec, xcraft
from xcraft import remote
from xcraft.build import BuildError, prepare_tree, source_path
from xcraft.container import run
from xcraft.lock import load_lock
from xcraft.project import Project
from xcraft.signing import check_signatures, create_dev_key
from xcraft.sources import cache_source, sha256_of, write_manifest

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.skipif(not shutil.which("podman"), reason="needs podman")

GARAGE = "docker.io/dxflrs/garage:v2.1.0"
# a garage for each worker of pytest-xdist, with its name and ports
WORKER = int(os.environ.get("PYTEST_XDIST_WORKER", "gw0").removeprefix("gw"))
CONTAINER = f"xcraft-test-garage-{WORKER}"
S3_PORT, WEB_PORT = 13900 + 10 * WORKER, 13902 + 10 * WORKER
WEB = f"http://xcraft.web.garage.localhost:{WEB_PORT}"


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
    + f'[remote]\nurl = "{WEB}"\ns3_endpoint = "http://localhost:{S3_PORT}"\nbucket = "xcraft"\n'
    # not the build repository, with the hundreds of source packages of their build environment to download
    '[publish]\nrelease = "9.0"\nbuild = false\n'
)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, s3: dict[str, str]) -> Path:
    root = tmp_path / "project"
    shutil.copytree(PROJECT, root)
    (root / "xcraft.toml").write_text(REMOTE)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    for name, value in s3.items():
        monkeypatch.setenv(name, value)
    return root


def test_sources(root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    archive = tmp_path / f"trivial-{secrets.token_hex(4)}.tar.gz"
    archive.write_text(archive.name)
    cache = Project.find().source_cache
    digest = cache_source(cache, archive)
    write_manifest(root / "base/trivial/sources", {archive.name: digest})

    code, output = xcraft("push", "sources", "--check")
    assert code == 1
    assert f"✗ {archive.name} is not in the remote storage" in output
    with caplog.at_level(logging.DEBUG, logger="xcraft"):  # what xcraft --debug shows
        assert xcraft("push", "sources")[1].endswith("1 pushed, 0 already in the remote storage\n")
    assert (
        f"uploading {cache / digest / archive.name} to s3://xcraft/sources/{digest}/{archive.name}" in caplog.messages
    )
    assert xcraft("push", "sources", "--check") == (0, "1 archives in the remote storage, 0 missing\n")

    # a fresh checkout downloads it
    shutil.rmtree(cache)
    assert (prepare_tree(Project.find(), "trivial") / "SOURCES" / archive.name).read_text() == archive.name
    shutil.rmtree(cache)
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        assert xcraft("fetch", "sources") == (0, "all the source archives are in the source cache\n")
    assert any(m.startswith(f"downloading {WEB}/sources/{digest}/") for m in caplog.messages)
    assert (cache / digest / archive.name).read_text() == archive.name
    # and checks it
    shutil.rmtree(cache)
    storage = Project.find().config.remote
    assert storage
    remote.upload(storage, source_path(archive.name, digest), root / "xcraft.toml")
    with pytest.raises(BuildError, match="does not match the sha256 of the manifest"):
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


def test_builds_shared_by_checkouts(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a key of its own, not to find the builds of the other tests in the storage
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    assert xcraft("build", "trivial")[0] == 0
    assert xcraft("push", "builds", "trivial")[0] == 0
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
    assert xcraft("build", "trivial")[0] == 0
    code, output = xcraft("push")
    # all that can be pushed, and then what could not
    assert code == 1
    assert f"↑ {archive.name}\n1 pushed, 0 already in the remote storage\n↑ trivial\n1 pushed, 0 already" in output
    assert re.search(r"\n\d+ pages\nnot built: with-deps$", output)
    assert xcraft("push", "sources", "--check")[0] == 0


def test_builds(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    assert xcraft("push", "builds", "trivial")[1] == "↑ trivial\n1 pushed, 0 already in the remote storage\n"
    # browsable, as the storage does not list directories
    assert 'href="builds/"' in page("")
    assert 'href="trivial/"' in page(f"builds/{ARCH}/")
    # with the size of all that is below them and the date of the newest file, from their pages when only their
    # parent is listed
    assert re.search(
        r'href="builds/">builds/</a></td><td data-k="[1-9]\d*"[^>]*>[\d.]+ \w+</td><td data-k="\d{4}-', page("")
    )
    assert xcraft("push", "index")[1].endswith(" pages\n")
    assert xcraft("push", "builds") == (1, "0 pushed, 1 already in the remote storage\nnot built: with-deps")

    # a fresh checkout downloads it, with xcraft fetch, or instead of building it
    shutil.rmtree(root / ".xcraft/cache/builds")
    assert xcraft("fetch", "builds") == (0, "↓ trivial\n1 fetched, 0 already cached, 1 not in the remote storage\n")
    shutil.rmtree(root / ".xcraft/cache/builds")
    assert xcraft("build", "with-deps", "--dry-run")[1].splitlines()[:2] == ["fetch    trivial", "build    with-deps"]
    assert xcraft("build", "with-deps", "--dry-run", "--no-fetch")[1].splitlines()[:2] == [
        "build    trivial",
        "build    with-deps",
    ]
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert output.splitlines()[0] == "↓ trivial fetched"
    assert output.splitlines()[-1] == "1 built, 1 fetched"
    assert list(
        (root / f".xcraft/cache/builds/{ARCH}/trivial").glob("*/RPMS/noarch/trivial-1.0-1.xcpng8.99.noarch.rpm")
    )
    # built, even though the storage has it
    shutil.rmtree(root / f".xcraft/cache/builds/{ARCH}/trivial")
    code, output = xcraft("build", "trivial", "--no-fetch")
    assert code == 0, output
    assert output.splitlines()[-1] == "1 built"


def ccache_hits(cache: Path) -> int:
    """Return how many compilations were found in the compiler cache `cache`."""
    run(Project.find(), "test-ccache", "CCACHE_DIR=/cache ccache --print-stats", volumes=[f"{cache}:/cache"])
    log = (Project.find().state / "logs/_test-ccache.log").read_text()
    stats = {k: int(v) for k, v in (line.split("\t") for line in log.splitlines() if "\t" in line)}
    return stats["direct_cache_hit"] + stats["preprocessed_cache_hit"]


def test_ccache(root: Path) -> None:
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
    assert xcraft("push", "ccache", "compiled") == (0, "↑ compiled\n1 pushed\n")
    shutil.rmtree(cache)
    assert xcraft("fetch", "ccache", "compiled") == (0, "↓ compiled\n1 fetched\n")
    assert sorted(cache.rglob("*R")) == results
    # a build without a compiler cache gets the one of the remote storage, and uses it, unless --no-fetch
    for release, options, hits in (("2", ["--no-fetch"], 0), ("3", [], 1)):
        shutil.rmtree(cache)
        spec.write_text(re.sub(r"Release: \d+", f"Release: {release}", spec.read_text()))
        assert xcraft("build", "compiled", *options)[0] == 0
        assert ccache_hits(cache) == hits


def test_unavailable_remote_storage(root: Path) -> None:
    (root / "xcraft.toml").write_text(REMOTE.replace(str(WEB_PORT), "13899"))  # of no worker
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert output.startswith("remote storage unavailable, building locally: http://xcraft.web.garage.localhost:13899/")
    assert output.splitlines()[-1] == "1 built"


def test_push_needs_the_credentials(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    # not the build the other tests pushed: the same key everywhere
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "no credentials"'))
    assert xcraft("build", "trivial")[0] == 0
    code, output = xcraft("push", "builds", "trivial")
    assert code == 1
    assert "pushing needs the S3 credentials" in output


def test_publish(root: Path) -> None:
    # the architectures the host runs, built for all: two on x86_64
    arches = [ARCH, "x86_64"] if ARCH == "x86_64_v2" else [ARCH]
    platforms = ", ".join(f'"{p}"' for p in [PLATFORM, "linux/amd64"][: len(arches)])
    (root / "xcraft.toml").write_text(REMOTE.replace(f'["{PLATFORM}"]', f"[{platforms}]") + 'extra = ["bc"]\n')
    code, output = xcraft("publish", "--unsigned")
    assert code == 0, output
    lock = load_lock(Project.find())
    assert {"bc", "glibc"} <= set(lock.runtime)
    assert lock.packages["bc"].repo == "baseos"
    storage = Project.find().config.remote
    assert storage
    day = datetime.now(UTC).date().isoformat()
    for name in (day, "latest"):
        base = f"repos/9.0/{name}/base"
        for repo in (*arches, "Source"):
            assert remote.exists(storage, f"{base}/{repo}/repodata/repomd.xml")
            assert remote.exists(
                storage, f"{base}/{repo}/trivial-1.0-1.xcpng8.99.{'src' if repo == 'Source' else 'noarch'}.rpm"
            )
        # the sources of the upstream packages too
        assert remote.exists(storage, f"{base}/Source/bc-{lock.packages['bc'].evr}.src.rpm")
        assert 'href="trivial-1.0-1.xcpng8.99.noarch.rpm"' in page(f"{base}/{arches[-1]}/")
    # a second one the same day, without changing the locks
    code, output = xcraft("publish", "--locked", "--unsigned")
    assert code == 0, output
    assert f"published repos/9.0/{day}.2, and as repos/9.0/latest" in output
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
    code, output = xcraft("publish", "--locked", "--local")
    assert code == 1
    assert re.search(r"--locked forbids: .*-runtime bc\b", output)
    # the upstream packages must be signed with the keys of the repositories
    (root / "xcraft.toml").write_text(f'{REMOTE}extra = ["bc"]\n')
    srpm = root / f".xcraft/cache/srpms/bc-{lock.packages['bc'].evr}.src.rpm"
    shutil.copy(next(root.glob(f".xcraft/cache/builds/{ARCH}/trivial/*/SRPMS/*.src.rpm")), srpm)
    code, output = xcraft("publish", "--local")
    assert code == 1
    assert f"✗ base/Source/{srpm.name}: digests SIGNATURES NOT OK" in output
    assert "some upstream packages are not signed with the keys of the repositories" in output


@pytest.mark.skipif(not shutil.which("gpg"), reason="needs gpg")
def test_signed_publish(root: Path) -> None:
    code, output = xcraft("publish")
    assert code == 2
    assert "no key to sign the packages" in " ".join(output.replace("│", "").split())
    signer = create_dev_key(Project.find(), "Release <release@example.com>")
    try:
        # the developer's key, without a release one
        code, output = xcraft("publish", "--local")
        assert code == 0, output
        assert f"warning: no release key in xcraft.toml, signing with the developer's key {signer.key}" in output
        assert (root / ".xcraft/publish/RPM-GPG-KEY").read_text() == signer.public_key()
        # nor for a storage on another machine, unless asked
        (root / "xcraft.toml").write_text(REMOTE.replace(f"localhost:{S3_PORT}", f"192.0.2.1:{S3_PORT}"))
        code, output = xcraft("publish")
        assert code == 2
        assert "or use --dev-key or --unsigned" in " ".join(output.replace("│", "").split())
        # but for one on this machine
        (root / "xcraft.toml").write_text(REMOTE)
        code, output = xcraft("publish")
        assert code == 0, output
        assert f"signing with the developer's key {signer.key}, the storage being on this machine" in output
        (root / "xcraft.toml").write_text(f'{REMOTE}[sign]\nkey = "{signer.key}"\ngnupghome = "{signer.gnupghome}"\n')
        code, output = xcraft("publish")
        assert code == 0, output
        snapshot = root / ".xcraft/publish"
        assert (snapshot / "RPM-GPG-KEY").read_text() == signer.public_key()
        for repo in (ARCH, "Source"):
            assert (snapshot / "base" / repo / "repodata/repomd.xml.asc").exists()
        ours = [f"base/{ARCH}/trivial-1.0-1.xcpng8.99.noarch.rpm", "base/Source/trivial-1.0-1.xcpng8.99.src.rpm"]
        check_signatures(Project.find(), snapshot, ours, what="packages of ours", public_key=signer.public_key())
        storage = Project.find().config.remote
        assert storage
        assert remote.exists(storage, f"repos/9.0/latest/base/{ARCH}/repodata/repomd.xml.asc")
        signed = list((root / ".xcraft/cache/signed" / signer.key / "trivial").iterdir())
        assert remote.tree_exists(storage, f"signed/{signer.key}/{ARCH}/trivial/{signed[0].name}")
        # signed once: the next snapshots have the same files
        rpm = signed[0] / "RPMS/noarch/trivial-1.0-1.xcpng8.99.noarch.rpm"
        before = rpm.read_bytes()
        assert xcraft("publish")[0] == 0
        assert rpm.read_bytes() == before
    finally:
        subprocess.run(["gpgconf", "--homedir", str(signer.gnupghome), "--kill", "gpg-agent"], check=False)


def test_publish_rebuilt_package(root: Path, tmp_path: Path) -> None:
    """A rebuild keeping its version-release is published, not the previous build of the storage."""
    assert xcraft("publish", "--unsigned")[0] == 0
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello, rebuilt"'))
    code, output = xcraft("publish", "--unsigned")
    assert code == 0, output
    storage = Project.find().config.remote
    assert storage
    rpm = f"base/{ARCH}/trivial-1.0-1.xcpng8.99.noarch.rpm"
    local = root / ".xcraft/publish" / rpm
    for name in (re.search(r"published (repos/\S+),", output)[1], "repos/9.0/latest"):  # ty: ignore[not-subscriptable]
        assert remote.read_manifest(storage, name)[rpm] == sha256_of(local)
        assert remote.download(storage, f"{name}/{rpm}", tmp_path / name.replace("/", "_"))
        assert sha256_of(tmp_path / name.replace("/", "_")) == sha256_of(local)


def test_manifest_paths_stay_in_their_tree(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote
    assert storage
    for name in ("../escaped", "/var/escaped", "a/../../escaped"):
        remote.write_manifest(storage, "trees/bad", {name: "0" * 64})
        with pytest.raises(remote.RemoteError, match="has a path out of its tree"):
            remote.download_tree(storage, "trees/bad", tmp_path / "tree")
    assert not list(tmp_path.glob("**/escaped"))


def test_pages_of_nested_directories(root: Path, tmp_path: Path) -> None:
    storage = Project.find().config.remote
    assert storage
    (tmp_path / "file").write_bytes(b"x" * 1000)
    remote.upload(storage, "nested/a/b/file", tmp_path / "file")
    remote.update_pages(storage, ["nested/a/", "nested/"])
    assert 'href="b/">b/</a></td><td data-k="1000"' in page("nested/a/")
    assert 'href="a/">a/</a></td><td data-k="1000"' in page("nested/")
