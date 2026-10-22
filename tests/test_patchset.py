from __future__ import annotations

import re
import subprocess
from typing import TYPE_CHECKING

import pytest

from xcraft.patchset import BEGIN, END, PatchsetError, guess_base, guess_package, repo_name, update_patchset
from xcraft.specs import Binary, Package

if TYPE_CHECKING:
    from pathlib import Path

SPEC = """\
Name: foo
Version: 1.0
Source0: foo-1.0.tar.gz
Patch0: upstream-fix.patch
# XCP-ng patches, exported by hand
Patch1000: 0001-first-change.patch
Patch1001: 0002-second-change.patch

%prep
%autosetup -p1
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, subject: str) -> None:
    (repo / "file").write_text(f"{(repo / 'file').read_text() if (repo / 'file').exists() else ''}{subject}\n")
    git(repo, "add", "file")
    git(repo, "commit", "-qm", subject)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "foo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    commit(repo, "upstream")
    git(repo, "tag", "v1.0")
    git(repo, "checkout", "-qb", "1.0-xcpng")
    commit(repo, "first change")
    commit(repo, "second change")
    return repo


@pytest.fixture
def pkg(tmp_path: Path) -> Path:
    pkg = tmp_path / "specs/foo"
    pkg.mkdir(parents=True)
    (pkg / "foo.spec").write_text(SPEC)
    for name, subject in (("0001-first-change.patch", "first change"), ("0002-second-change.patch", "second change")):
        (pkg / name).write_text(f"From 1234 Mon Sep 17 00:00:00 2001\nSubject: [PATCH] {subject}\n\nold export\n")
    (pkg / "upstream-fix.patch").write_text("Subject: [PATCH] upstream fix\n")
    return pkg


def patches(spec: str) -> list[tuple[str, str]]:
    return re.findall(r"^Patch(\d+): (\S+)$", spec, re.MULTILINE)


def test_first_update_replaces_previous_exports(repo: Path, pkg: Path) -> None:
    result = update_patchset(pkg, pkg / "foo.spec", repo, "v1.0", 1000)
    assert result.replaced == ["0001-first-change.patch", "0002-second-change.patch"]
    spec = (pkg / "foo.spec").read_text()
    assert patches(spec) == [
        ("0", "upstream-fix.patch"),
        ("1000", "0001-first-change.patch"),
        ("1001", "0002-second-change.patch"),
    ]
    head = git(repo, "rev-parse", "--short=12", "HEAD")
    assert f"{BEGIN}{repo} 1.0-xcpng v1.0..{head}\n" in spec  # no origin remote: its path
    assert spec.count(END) == 1
    assert "# XCP-ng patches, exported by hand\n" in spec  # comments are kept
    assert "From 0000000000000000000000000000000000000000" in (pkg / "0001-first-change.patch").read_text()


def test_update_after_rebase(repo: Path, pkg: Path) -> None:
    update_patchset(pkg, pkg / "foo.spec", repo, "v1.0", 1000)
    git(repo, "reset", "-q", "--hard", "HEAD~1")
    commit(repo, "third change")
    result = update_patchset(pkg, pkg / "foo.spec", repo, "v1.0", 1000)
    assert (result.added, result.removed) == (["0002-third-change.patch"], ["0002-second-change.patch"])
    assert sorted(p.name for p in pkg.iterdir()) == [
        "0001-first-change.patch",
        "0002-third-change.patch",
        "foo.spec",
        "upstream-fix.patch",
    ]
    assert patches((pkg / "foo.spec").read_text())[1:] == [
        ("1000", "0001-first-change.patch"),
        ("1001", "0002-third-change.patch"),
    ]


def test_update_errors(repo: Path, pkg: Path) -> None:
    (pkg / "foo.spec").write_text(SPEC.replace("Patch1001: 0002", "Patch1002: 0002").replace("0002-second", "other"))
    with pytest.raises(PatchsetError, match="patch numbers 1001-1002 are used by other patches"):
        update_patchset(pkg, pkg / "foo.spec", repo, "v1.0", 1001)
    (pkg / "foo.spec").write_text(SPEC.replace("%autosetup -p1", "%setup -q\n%patch -P0 -p1"))
    with pytest.raises(PatchsetError, match="applies its patches explicitly"):
        update_patchset(pkg, pkg / "foo.spec", repo, "v1.0", 1000)
    # an unrelated patch with the name of an exported one
    (pkg / "foo.spec").write_text(SPEC)
    (pkg / "0001-first-change.patch").write_text("Subject: [PATCH] unrelated\n")
    with pytest.raises(PatchsetError, match=r"would overwrite files of the package .*: 0001-first-change\.patch"):
        update_patchset(pkg, pkg / "foo.spec", repo, "v1.0", 2000)
    assert (pkg / "0001-first-change.patch").read_text() == "Subject: [PATCH] unrelated\n"
    assert (pkg / "foo.spec").read_text() == SPEC


def test_guesses(repo: Path, pkg: Path) -> None:
    git(repo, "remote", "add", "origin", "git@github.com:xcp-ng/foo.git")
    assert repo_name(repo) == "foo"
    assert guess_base(repo, "1.0") == "v1.0"
    with pytest.raises(PatchsetError, match="give the base of the patchset with --base"):
        guess_base(repo, "2.0")
    binary = Binary(name="foo-tools", evr="1.0-1", arch="noarch", provides=[], requires=[])
    packages = {"bar": Package(name="bar", binaries=[binary], source0="https://example.com/v1.0/foo-1.0.tar.gz")}
    assert guess_package("foo", packages) == "bar"
    assert guess_package("baz", packages) is None
