from __future__ import annotations

import hashlib
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from tests import TOML, config
from xcraft.__main__ import app
from xcraft.container import run
from xcraft.importer import import_package
from xcraft.project import Project
from xcraft.sources import read_manifest, sha256_of

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit_files(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "update")


@pytest.fixture
def packaging_repo(tmp_path: Path) -> Path:
    """Create a packaging repository with its archive in LFS, on master and ydi/9."""
    repo = tmp_path / "foo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    git(repo, "lfs", "install", "--local")
    git(repo, "lfs", "track", "*.tar.gz")
    commit_files(
        repo,
        {"SPECS/foo.spec": "Name: foo\n", "SOURCES/foo-1.0.tar.gz": "archive 1.0\n", "SOURCES/fix.patch": "fix\n"},
    )
    git(repo, "checkout", "-qb", "ydi/9")
    git(repo, "rm", "-q", "SOURCES/fix.patch")
    commit_files(repo, {"SPECS/foo.spec": "Name: foo\n# ydi/9\n", "SOURCES/foo-1.0.tar.gz": "archive 9\n"})
    git(repo, "checkout", "-q", "master")
    return repo


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / "base").mkdir(parents=True)
    (root / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


def test_import(root: Path, packaging_repo: Path) -> None:
    url = f"file://{packaging_repo}"
    result = runner.invoke(app, ["import", "foo", "--url", url])
    assert result.exit_code == 0, result.output

    pkg = root / "base/foo"
    assert sorted(p.name for p in pkg.iterdir()) == ["fix.patch", "foo.spec", "origin", "sources"]
    digest = read_manifest(pkg / "sources")["foo-1.0.tar.gz"]
    assert (root / ".xcraft/cache/sources" / digest / "foo-1.0.tar.gz").read_text() == "archive 1.0\n"
    assert sha256_of(root / ".xcraft/cache/sources" / digest / "foo-1.0.tar.gz") == digest
    assert (pkg / "origin").read_text() == f"{url} master {git(packaging_repo, 'rev-parse', 'master')}\n"


def test_reimport_all_keeps_origin_branch(root: Path, packaging_repo: Path) -> None:
    url = f"file://{packaging_repo}"
    assert runner.invoke(app, ["import", "foo", "--url", url, "--branch", "ydi/9"]).exit_code == 0
    (root / "base/foo/stale").write_text("")

    result = runner.invoke(app, ["import", "--all"])
    assert result.exit_code == 0, result.output

    pkg = root / "base/foo"
    assert sorted(p.name for p in pkg.iterdir()) == ["foo.spec", "origin", "sources"]
    assert (pkg / "foo.spec").read_text() == "Name: foo\n# ydi/9\n"
    assert (pkg / "origin").read_text().split()[1] == "ydi/9"


def test_import_in_another_repository(root: Path, packaging_repo: Path) -> None:
    url = f"file://{packaging_repo}"
    result = runner.invoke(app, ["import", "foo", "--url", url, "--repo", "xostor"])
    assert "xostor is not a repository of the project: base" in result.output
    (root / "xcraft.toml").write_text(f'repositories = ["base", "xostor"]\n{TOML}')
    assert runner.invoke(app, ["import", "foo", "--url", url, "--repo", "xostor"]).exit_code == 0
    assert (root / "xostor/foo/foo.spec").exists()
    # where it is, on a re-import
    assert runner.invoke(app, ["import", "--all"]).exit_code == 0
    assert sorted(p.relative_to(root).as_posix() for p in root.glob("[!.]*/*")) == ["xostor/foo"]


def test_url_needs_a_single_package(root: Path) -> None:
    result = runner.invoke(app, ["import", "foo", "bar", "--url", "file:///nowhere"])
    assert result.exit_code != 0
    assert "--url applies to a single package" in result.output


def test_import_dist_git_layout(tmp_path: Path) -> None:
    lookaside = tmp_path / "lookaside"
    lookaside.mkdir()
    archive = b"grub archive\n"
    sha512 = hashlib.sha512(archive).hexdigest()
    (lookaside / sha512).write_bytes(archive)
    repo = tmp_path / "grub"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "ydi/9")
    commit_files(
        repo,
        {
            "grub2.spec": "Name: grub2\n",
            "fix.patch": "fix\n",
            ".gitignore": "*.tar.xz\n",
            "sources": f"SHA512 (grub-2.12.tar.xz) = {sha512}\n",
        },
    )

    project = Project(tmp_path, config())
    pkg = tmp_path / "base/grub"
    import_package(project, pkg, f"file://{repo}", "ydi/9", lookaside=f"file://{lookaside}/{{checksum}}")
    assert sorted(p.name for p in pkg.iterdir()) == ["fix.patch", "grub2.spec", "origin", "sources"]
    digest = read_manifest(pkg / "sources")["grub-2.12.tar.xz"]
    assert (project.source_cache / digest / "grub-2.12.tar.xz").read_bytes() == archive

    # downloaded again, not being in the cache anymore
    (lookaside / sha512).write_bytes(b"corrupted\n")
    shutil.rmtree(project.source_cache)
    with pytest.raises(RuntimeError, match="does not match its SHA512"):
        import_package(project, pkg, f"file://{repo}", "ydi/9", lookaside=f"file://{lookaside}/{{checksum}}")


@pytest.mark.skipif(not shutil.which("podman"), reason="needs podman")
def test_import_source_rpm(root: Path, tmp_path: Path) -> None:
    top = tmp_path / "rpmbuild"
    (top / "SOURCES").mkdir(parents=True)
    (top / "SOURCES/foo-1.0.tar.gz").write_text("archive\n")
    (top / "SOURCES/fix.patch").write_text("fix\n")
    (top / "foo.spec").write_text(
        "Name: foo\nVersion: 1.0\nRelease: 1\nSummary: foo\nLicense: MIT\n"
        "Source0: foo-1.0.tar.gz\nPatch0: fix.patch\n%description\nfoo\n"
    )
    # in the build environment: rpmbuild is not on every host
    run(Project.find(), "test-srpm", "rpmbuild -bs -D '_topdir /top' /top/foo.spec", volumes=[f"{top}:/top"])
    srpm = next((top / "SRPMS").glob("*.src.rpm"))
    pkg = root / "base/foo"
    pkg.mkdir(parents=True)
    (pkg / "stale.patch").write_text("stale\n")
    checksum = import_package(Project.find(), pkg, f"file://{srpm}", "ignored")
    assert checksum == sha256_of(srpm)
    assert sorted(p.name for p in pkg.iterdir()) == ["fix.patch", "foo.spec", "origin", "sources"]
    assert read_manifest(pkg / "sources") == {"foo-1.0.tar.gz": sha256_of(top / "SOURCES/foo-1.0.tar.gz")}
    assert (pkg / "origin").read_text() == f"file://{srpm} srpm {checksum}\n"


def test_import_refuses_what_escapes_the_package(root: Path, tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "master")
    commit_files(repo, {"SPECS/foo.spec": "Name: foo\n", "SOURCES/fix.patch": "fix\n"})
    pkg = root / "base/foo"
    import_package(Project.find(), pkg, f"file://{repo}", "master")
    origin = (pkg / "origin").read_text()

    # a link to a file of the host, which a failed import does not touch the package for
    (repo / "SOURCES/key").symlink_to(tmp_path / "secret")
    (tmp_path / "secret").write_text("secret")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "link")
    with pytest.raises(RuntimeError, match="SOURCES/key: not a regular file, not imported"):
        import_package(Project.find(), pkg, f"file://{repo}", "master")
    assert (pkg / "origin").read_text() == origin
    assert sorted(p.name for p in root.joinpath("base").iterdir()) == ["foo"]

    # an archive name out of the package, in a dist-git sources file
    dist_git = tmp_path / "dist-git"
    dist_git.mkdir()
    git(dist_git, "init", "-q", "-b", "master")
    commit_files(dist_git, {"bar.spec": "Name: bar\n", "sources": f"SHA512 (../escaped) = {'0' * 128}\n"})
    with pytest.raises(RuntimeError, match="not a file name, in the sources file"):
        import_package(Project.find(), root / "base/bar", f"file://{dist_git}", "master")
    assert not (root / "base/bar").exists()

    result = runner.invoke(app, ["import", "..", "--url", f"file://{repo}"])
    assert result.exit_code == 2
    assert "not package names: '..'" in result.output
