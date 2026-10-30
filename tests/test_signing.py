"""Signing our packages: the developer's key for development builds, and the release key for publishing."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
import typer

from tests import ARCH, copy_project
from tests.test_build import xcraft
from xcraft import buildid
from xcraft.cache import build_keys, cached_build
from xcraft.commands import choose_signer
from xcraft.project import Project, Sign
from xcraft.publish import _signed_build
from xcraft.signing import Signer, SigningError, check_signatures, create_dev_key, dev_keyring, dev_signer, signing_key

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

needs_gpg = pytest.mark.skipif(not shutil.which("gpg"), reason="needs gpg")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "project"
    copy_project(root)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
        subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    yield root
    # the gpg agent of the keyring
    subprocess.run(["gpgconf", "--homedir", str(root / ".xcraft/key"), "--kill", "gpg-agent"], check=False)


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
    assert signing_key(project) == developer
    assert choose_signer(project, local=False, unsigned=False, dev_key=False) == developer
    assert signing_key(project, "A" * 40) == Signer("A" * 40)
    project.config.sign = Sign(key="B" * 40, gnupghome=str(root / "release-keyring"))
    assert signing_key(project) == Signer("B" * 40, root / "release-keyring")
    assert signing_key(project, "A" * 40) == Signer("A" * 40, root / "release-keyring")
    # fingerprints, in uppercase, like the storage has the public keys
    assert signing_key(project, "a" * 40) == Signer("A" * 40, root / "release-keyring")
    with pytest.raises(SigningError, match="not the fingerprint of a key"):
        signing_key(project, "Developer")
    assert Sign(key="b" * 40).key == "B" * 40
    with pytest.raises(ValueError, match="not the fingerprint of a key"):
        Sign(key="B" * 39)
    assert choose_signer(project, local=True, unsigned=False, dev_key=True) == developer
    assert choose_signer(project, local=True, unsigned=True, dev_key=False) is None
    for unsigned, dev_key, key in ((False, True, "A" * 40), (True, False, "A" * 40), (True, True, None)):
        with pytest.raises(typer.BadParameter, match="cannot be used"):
            choose_signer(project, local=True, unsigned=unsigned, dev_key=dev_key, key=key)


@needs_gpg
def test_development_builds_are_signed(build_environment: None, root: Path) -> None:
    def rpms(*args: str) -> list[Path]:
        code, output = xcraft(*args)
        assert code == 0, output
        return [root / p for p in output.splitlines() if p.endswith(".rpm")]

    # a change on a branch: development builds
    subprocess.run(["git", "-C", str(root), "symbolic-ref", "HEAD", "refs/heads/master"], check=True)
    buildid.update(Project.find())
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
    subprocess.run(["git", "-C", str(root), "checkout", "-qb", "feature"], check=True)
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# changed\n{spec.read_text()}")
    # unsigned without a key
    unsigned = rpms("build", "trivial", "--dev", "nokey")
    assert xcraft("key", "create")[0] == 0
    signer = dev_signer(Project.find())
    assert signer
    signed = rpms("build", "trivial", "--dev", "key")
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
            root,
            [str(p.relative_to(root)) for p in unsigned],
            what="test",
            public_key=signer.public_key(),
        )

    # their repositories too, with the key for the test hosts
    assert xcraft("repo", "trivial", "--dev", "key")[0] == 0
    repo = root / "repo-key"
    assert (repo / "repodata/repomd.xml.asc").exists()
    assert (repo / "RPM-GPG-KEY-xcraft-test").read_text() == signer.public_key()
    assert "gpgcheck=1\nrepo_gpgcheck=1\ngpgkey=file://" in (repo / "xcraft-key.repo").read_text()


@pytest.mark.skipif(ARCH != "x86_64_v2", reason="two architectures the host runs: x86_64_v2 and x86_64")
def test_signed_builds_of_each_architecture(root: Path, build_environment: None) -> None:
    config = root / "xcraft.toml"
    config.write_text(config.read_text().replace('"linux/arm64"', '"linux/amd64"'))
    assert xcraft("build", "trivial")[0] == 0
    project, other = Project.find(), Project.find("x86_64")
    signer = create_dev_key(project, "Developer")
    # the same build key on both: another build for the other architecture, told apart by a file
    key = build_keys(project)["trivial"]
    shutil.copytree(cached_build(project, "trivial", key), cached_build(other, "trivial", key))
    (cached_build(other, "trivial", key) / "RPMS/x86_64").write_text("the build of x86_64")
    signed = [_signed_build(p, signer, "trivial", key, storage=None) for p in (project, other)]
    assert not (signed[0] / "RPMS/x86_64").exists()
    assert (signed[1] / "RPMS/x86_64").read_text() == "the build of x86_64"
