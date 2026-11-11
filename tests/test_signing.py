"""Signing our packages: the developer's key for development builds, and the release key for publishing."""

from __future__ import annotations

import http.server
import shutil
import subprocess
import threading
from contextlib import contextmanager
from functools import partial
from typing import TYPE_CHECKING

import pytest
import typer

from tests import ARCH, PROJECT, TOYS, config, copy_project, needs_gpg
from tests.test_build import xcraft
from xcraft import buildid, pinned, publish
from xcraft.cache import build_keys, cached_build
from xcraft.commands import choose_signer
from xcraft.lock import cached_lock
from xcraft.project import Config, Project, Remote, Repo, Sign
from xcraft.publish import _signed_build
from xcraft.signing import Signer, SigningError, check_signatures, create_dev_key, dev_keyring, dev_signer, key_signer

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator
    from pathlib import Path


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Iterator[Path]:
    root = tmp_path / "project"
    # prepared for the tests in the build environment
    prepared = "build_environment" in request.fixturenames
    copy_project(root, request.getfixturevalue("build_environment") if prepared else PROJECT)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    yield root
    if shutil.which("gpgconf"):  # the gpg agent of the keyring
        subprocess.run(["gpgconf", "--homedir", str(root / ".xcraft/key"), "--kill", "gpg-agent"], check=False)


@needs_gpg
def test_dev_key(root: Path) -> None:
    assert xcraft("key", "show") == (1, "no key: see xcraft key create\n")
    code, output = xcraft("key", "create")
    assert code == 0, output
    assert "for: Test User (xcraft development builds) <test@example.com>" in output
    signer = dev_signer(Project.find())
    assert signer
    assert xcraft("key", "show")[1].startswith(f"{signer.key}\n")
    assert xcraft("key", "show", "--export")[1].startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert xcraft("key", "create")[1] == f"there is already a key in {dev_keyring(Project.find())}"


@needs_gpg
def test_signer_selection(root: Path) -> None:
    project = Project.find()
    developer = create_dev_key(project, "Developer")
    assert key_signer(project) == developer
    assert choose_signer(project, local=False, unsigned=False, dev_key=False) == developer
    assert key_signer(project, "A" * 40) == Signer("A" * 40)
    project.config.sign = Sign(key="B" * 40, gnupghome=str(root / "release-keyring"))
    assert key_signer(project) == Signer("B" * 40, root / "release-keyring")
    assert key_signer(project, "A" * 40) == Signer("A" * 40, root / "release-keyring")
    # fingerprints, in uppercase, like the storage has the public keys
    assert key_signer(project, "a" * 40) == Signer("A" * 40, root / "release-keyring")
    with pytest.raises(ValueError, match="not the fingerprint of a key"):
        key_signer(project, "Developer")
    assert Sign(key="b" * 40).key == "B" * 40
    with pytest.raises(ValueError, match="not the fingerprint of a key"):
        Sign(key="B" * 39)
    assert choose_signer(project, local=True, unsigned=False, dev_key=True) == developer
    assert choose_signer(project, local=True, unsigned=True, dev_key=False) is None
    for unsigned, dev_key, key in ((False, True, "A" * 40), (True, False, "A" * 40), (True, True, None)):
        with pytest.raises(typer.BadParameter, match="cannot be used"):
            choose_signer(project, local=True, unsigned=unsigned, dev_key=dev_key, key=key)


@contextmanager
def _served(directory: Path) -> Generator[str]:
    """Serve `directory` over HTTP, and return its URL."""

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    with http.server.ThreadingHTTPServer(("localhost", 0), partial(Handler, directory=directory)) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://localhost:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def test_repo_keys_of_urls_pinned() -> None:
    assert Repo(baseurl="file:///repo", gpgkey="file:///etc/pki/rpm-gpg/key").fingerprints == []
    with pytest.raises(ValueError, match="needs the fingerprints of its keys"):
        Repo(baseurl="file:///repo", gpgkey="file:///etc/pki/rpm-gpg/key https://example.com/key")
    assert Repo(baseurl="file:///repo", gpgkey="https://example.com/key", fingerprints=["a" * 40]).fingerprints == [
        "A" * 40
    ]
    with pytest.raises(ValueError, match="not the fingerprint of a key"):
        Repo(baseurl="file:///repo", gpgkey="https://example.com/key", fingerprints=["a" * 16])


def test_no_build_keys_on_the_dev_bucket() -> None:
    dev = Remote(url="http://dev", build_keys=["A" * 40])
    with pytest.raises(ValueError, match=r"remote\.dev cannot have build_keys"):
        Config.model_validate({**config().model_dump(by_alias=True), "remote": {"dev": dev.model_dump()}})
    Config.model_validate({**config().model_dump(by_alias=True), "remote": {"main": dev.model_dump()}})


def _check_with_keys_at_urls(root: Path, tmp_path: Path, signer: Signer, signed: list[Path]) -> None:
    """Check the packages `signed` by `signer` with the keys of the repositories, at URLs which may give other keys:
    downloaded again only when the cached one is not of the fingerprints of its repository.
    """
    (served := tmp_path / "served").mkdir()
    (served / "key.asc").write_text(signer.public_key())
    with _served(served) as url:
        try:
            project = Project.find()
            repo = next(iter(project.config.repos.values()))
            keyed = repo.model_copy(update={"gpgkey": f"{url}/key.asc", "fingerprints": [signer.key]})
            project.config.repos = {"keyed": keyed}
            paths = [str(p.relative_to(root)) for p in signed]
            check_signatures(project, root, paths, what="test")
            log = project.log("_signatures")
            checked = log.stat().st_mtime_ns
            check_signatures(project, root, paths, what="test")
            assert log.stat().st_mtime_ns == checked
            (keyring := tmp_path / "other").mkdir(mode=0o700)
            other = Signer("", keyring)
            other.gpg("--passphrase", "", "--quick-generate-key", "Other", "ed25519", "sign", "never")
            other.key = other.gpg("--with-colons", "--list-keys").split("\nfpr:")[1].split(":")[8]
            (served / "key.asc").write_text(other.gpg("--armor", "--export"))
            # the key cached, of its fingerprint, without the network
            check_signatures(project, root, paths, what="test")
            assert log.stat().st_mtime_ns == checked
            project.config.repos = {"keyed": keyed.model_copy(update={"fingerprints": ["B" * 40]})}
            with pytest.raises(SigningError, match=f"key.asc gives the keys {other.key}, not those of fingerprints"):
                check_signatures(project, root, paths, what="test")
            project.config.repos = {"keyed": keyed.model_copy(update={"fingerprints": [other.key]})}
            with pytest.raises(SigningError, match="some test are not signed with the keys of the repositories"):
                check_signatures(project, root, paths, what="test")
        finally:
            subprocess.run(["gpgconf", "--homedir", str(tmp_path / "other"), "--kill", "gpg-agent"], check=False)


@needs_gpg
def test_pinned_packages_are_signed(build_environment: Path, root: Path, tmp_path: Path) -> None:
    project = Project.find().on_host()
    image = {n: p for n, p in pinned.pins(project).items() if n in cached_lock(project).image[project.arch]}
    pinned.repository(project, image)  # not checked, as the toys have no key
    baseos = project.config.repos["baseos"]
    project.config.repos = {"baseos": baseos}
    pinned.check_signed(project, image)
    other = create_dev_key(project, "Other")
    (tmp_path / "key.asc").write_text(other.public_key())
    with _served(tmp_path) as url:
        keyed = baseos.model_copy(update={"gpgkey": f"{url}/key.asc", "fingerprints": [other.key]})
        project.config.repos = {"baseos": keyed}
        with pytest.raises(SigningError, match="some pinned packages are not signed with the keys of the repositories"):
            pinned.check_signed(project, image)


@needs_gpg
def test_development_builds_are_signed(build_environment: Path, root: Path, tmp_path: Path) -> None:
    # a change on a branch: development builds
    subprocess.run(["git", "-C", str(root), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
    buildid.update(Project.find())
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(root), "checkout", "-qb", "feature"], check=True)
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# changed\n{spec.read_text()}")
    # signed by the developer's key, which the first build makes
    code, output = xcraft("build", "trivial", "--dev", "key")
    assert code == 0, output
    assert "made the developer's key" in output
    signed = sorted(root.glob(".xcraft/cache/builds/*/*/trivial/*~*.key/RPMS/*/*.rpm"))
    signer = dev_signer(Project.find())
    assert signer
    assert signed
    assert xcraft("key", "create")[0] != 0  # there is one
    unsigned = sorted(TOYS.rglob("*.rpm"))[:1]  # the toys are not signed
    check_signatures(
        Project.find(), root, [str(p.relative_to(root)) for p in signed], what="test", public_key=signer.public_key()
    )
    log = Project.find().log("_signatures")
    checked = log.stat().st_mtime_ns
    moved = root / "moved"
    moved.mkdir()
    for rpm in signed:
        shutil.copy2(rpm, moved / rpm.name)
    check_signatures(Project.find(), moved, [p.name for p in signed], what="test", public_key=signer.public_key())
    assert log.stat().st_mtime_ns == checked
    check_signatures(
        Project.find(), moved, [p.name for p in signed], what="test", public_key=signer.public_key() + "\n"
    )
    assert log.stat().st_mtime_ns != checked
    shutil.copy2(unsigned[0], moved / signed[0].name)
    with pytest.raises(SigningError, match="some test are not signed with their key"):
        check_signatures(Project.find(), moved, [signed[0].name], what="test", public_key=signer.public_key())
    with pytest.raises(SigningError, match="some test are not signed with their key"):
        check_signatures(
            Project.find(),
            TOYS,
            [str(p.relative_to(TOYS)) for p in unsigned],
            what="test",
            public_key=signer.public_key(),
        )

    _check_with_keys_at_urls(root, tmp_path, signer, signed)

    # their repositories too, with the key for the test hosts
    assert xcraft("repo", "trivial", "--dev", "key")[0] == 0
    repo = root / "repo-key"
    assert (repo / "repodata/repomd.xml.asc").exists()
    assert (repo / "RPM-GPG-KEY-xcraft-test").read_text() == signer.public_key()
    assert "gpgcheck=1\nrepo_gpgcheck=1\ngpgkey=file://" in (repo / "xcraft-key.repo").read_text()


@pytest.mark.skipif(ARCH != "x86_64_v2", reason="two architectures the host runs: x86_64_v2 and x86_64")
def test_signed_builds_of_each_architecture(root: Path, build_environment: Path) -> None:
    config = root / "xcraft.toml"
    config.write_text(config.read_text().replace('"linux/arm64"', '"linux/amd64"'))
    assert xcraft("build", "trivial")[0] == 0
    project, other = Project.find(), Project.find("x86_64")
    signer = dev_signer(project)  # made by the build
    assert signer
    # the same build key on both: another build for the other architecture, told apart by a file
    key = build_keys(project)["trivial"]
    shutil.copytree(cached_build(project, "trivial", key), cached_build(other, "trivial", key))
    (cached_build(other, "trivial", key) / "RPMS/x86_64").write_text("the build of x86_64")
    signed = [_signed_build(p, signer, "trivial", key, storage=None) for p in (project, other)]
    assert not (signed[0] / "RPMS/x86_64").exists()
    assert (signed[1] / "RPMS/x86_64").read_text() == "the build of x86_64"


def test_final_builds_need_a_key(root: Path) -> None:
    code, output = xcraft("build", "trivial", "--final")
    assert code == 2
    assert "no key to sign the final builds" in " ".join(output.replace("│", "").split())
    assert not (root / ".xcraft/key").exists()  # made for dev mode only


@needs_gpg
def test_snapshots_of_dev_mode_keep_the_signatures_of_the_builds(
    build_environment: Path, root: Path, tmp_path: Path
) -> None:
    assert xcraft("build", "trivial")[0] == 0
    developer = dev_signer(Project.find())  # made by the build
    assert developer
    # another key signing the snapshot, like a stage's
    (keyring := tmp_path / "stage").mkdir(mode=0o700)
    stage = Signer(key="", gnupghome=keyring)
    stage.gpg("--passphrase", "", "--quick-generate-key", "Stage", "ed25519", "sign", "never")
    stage.key = stage.gpg("--with-colons", "--list-secret-keys").split("fpr:::::::::")[1].split(":")[0]
    try:
        project = Project.find()
        dest = tmp_path / "snapshot"
        publish.snapshot([project], dest, signer=stage, storage=None, build=False, keep=True)
        ours = [str(p.relative_to(dest)) for p in dest.glob("base/*/trivial-*.rpm")]
        assert ours
        check_signatures(project, dest, ours, what="ours", public_key=developer.public_key())  # not signed again
        assert (dest / "RPM-GPG-KEY").read_text() == stage.public_key()
        assert (dest / f"RPM-GPG-KEY-{developer.key}").read_text() == developer.public_key()
        assert (dest / f"base/{ARCH}/repodata/repomd.xml.asc").exists()
        # signed again in final mode
        publish.snapshot([project], dest, signer=stage, storage=None, build=False)
        check_signatures(project, dest, ours, what="ours", public_key=stage.public_key())
        assert not list(dest.glob("RPM-GPG-KEY-*"))
    finally:
        subprocess.run(["gpgconf", "--homedir", str(stage.gnupghome), "--kill", "gpg-agent"], check=False)
