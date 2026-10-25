"""Signing our packages: the developer's key for test builds, and the release key for publishing."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests import copy_project
from tests.test_build import xcraft
from xcraft.project import Project
from xcraft.signing import SigningError, check_signatures, dev_keyring, dev_signer

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
    assert "for: Test User (xcraft test builds) <test@example.com>" in output
    signer = dev_signer(Project.find())
    assert signer
    assert xcraft("key", "show")[1].startswith(f"{signer.key}\n")
    assert xcraft("key", "show", "--export")[1].startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----")
    assert xcraft("key", "create")[1] == f"there is already a key in {dev_keyring(Project.find())}"


@needs_gpg
def test_test_builds_are_signed(build_environment: None, root: Path) -> None:
    def rpms(*args: str) -> list[Path]:
        code, output = xcraft(*args)
        assert code == 0, output
        return [root / p for p in output.splitlines() if p.endswith(".rpm")]

    # unsigned without a key
    unsigned = rpms("build", "trivial", "--test", "nokey")
    assert xcraft("key", "create")[0] == 0
    signer = dev_signer(Project.find())
    assert signer
    signed = rpms("build", "trivial", "--test", "key")
    check_signatures(
        Project.find(), root, [str(p.relative_to(root)) for p in signed], what="test", public_key=signer.public_key()
    )
    with pytest.raises(SigningError, match="some test are not signed with their key"):
        check_signatures(
            Project.find(),
            root,
            [str(p.relative_to(root)) for p in unsigned],
            what="test",
            public_key=signer.public_key(),
        )

    # their repositories too, with the key for the test hosts
    assert xcraft("repo", "trivial", "--test", "key")[0] == 0
    repo = root / "repo-key"
    assert (repo / "repodata/repomd.xml.asc").exists()
    assert (repo / "RPM-GPG-KEY-xcraft-test").read_text() == signer.public_key()
    assert "gpgcheck=1\nrepo_gpgcheck=1\ngpgkey=file://" in (repo / "xcraft-key.repo").read_text()
