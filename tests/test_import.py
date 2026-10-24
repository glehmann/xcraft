from __future__ import annotations

import hashlib
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from tests import TOML
from xcraft.__main__ import app
from xcraft.container import run
from xcraft.importer import PackageImportError, import_history
from xcraft.project import Project
from xcraft.resolution import prepare

if TYPE_CHECKING:
    from pathlib import Path

runner = CliRunner()


def git(repo: Path, *args: str, input: str | None = None) -> str:  # noqa: A002, like subprocess.run
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        input=input,
    ).stdout.strip()


def commit_files(repo: Path, files: dict[str, str]) -> None:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "update")


@pytest.fixture
def upstream(tmp_path: Path) -> Path:
    """Create a packaging repository like those of xcp-ng-rpms: XS-9.0 has the source RPMs, merged into master."""
    repo = tmp_path / "rpms/foo"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "master")
    commit_files(repo, {"README.md": "foo\n"})
    git(repo, "lfs", "install", "--local")
    git(repo, "lfs", "track", "*.tar.gz")
    commit_files(repo, {"SPECS/foo.spec": "Name: foo\n", "SOURCES/foo-1.0.tar.gz": "1.0\n", "SOURCES/a.patch": "a\n"})
    git(repo, "checkout", "-qb", "XS-9.0")
    commit_files(repo, {"SPECS/foo.spec": "Name: foo\nVersion: 2.0\n", "SOURCES/foo-1.0.tar.gz": "2.0\n"})
    git(repo, "checkout", "-q", "master")
    # a commit of GitHub's web interface, without the LF ending the message, of what is not imported
    (repo / ".github").mkdir()
    (repo / ".github/CODEOWNERS").write_text("* @owner\n")
    git(repo, "add", "-A")
    tree = git(repo, "write-tree")
    author = "Owner <owner@example.com> 1700000000 +0000"
    raw = f"tree {tree}\nparent {git(repo, 'rev-parse', 'HEAD')}\nauthor {author}\ncommitter {author}\n\nSet owner"
    commit = git(repo, "hash-object", "-t", "commit", "-w", "--stdin", input=raw)
    git(repo, "reset", "-q", commit)
    (repo / "SOURCES/b.patch").write_text("b\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "Add b.patch")
    git(repo, "merge", "-q", "--no-edit", "XS-9.0")
    return repo


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    imports = '[import]\nbranches = ["master", "XS-9.0", "XS-10"]\nsrpm_branch = "XS-9.0"\n'
    imports += 'removed = ["Citrix_Logo_Black.png"]\n'
    (root / "xcraft.toml").write_text(f"{TOML}{imports}")
    git(root, "init", "-q", "-b", "master")
    for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
        git(root, "config", key, value)
    git(root, "commit", "-q", "--allow-empty", "-m", "start")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "the project")
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


def test_import(root: Path, upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"file://{upstream}"
    result = runner.invoke(app, ["import", "foo", "--url", url])
    assert result.exit_code == 0, result.output
    master, xs = git(root, "rev-parse", "import/foo/master"), git(root, "rev-parse", "import/foo/XS-9.0")
    assert (
        f"foo: import/foo/master at {master[:12]}\nfoo: import/foo/XS-9.0 at {xs[:12]}\n" in result.output
    )  # not XS-10
    # the files of SPECS/ and SOURCES/ in the package, and the archives in the manifest, by the SHA-256 of LFS
    files = ["base/foo/a.patch", "base/foo/b.patch", "base/foo/foo.spec", "base/foo/sources"]
    assert git(root, "ls-tree", "-r", "--name-only", master).split() == files
    digest = hashlib.sha256(b"2.0\n").hexdigest()
    assert git(root, "show", f"{master}:base/foo/sources") == f"{digest}  foo-1.0.tar.gz"
    # without the commits of the README and of the owner, from the first commit of the project
    assert git(root, "log", "--topo-order", "--format=%s", master).splitlines() == [
        "Merge branch 'XS-9.0'",
        "update",
        "Add b.patch",
        "update",
        "start",
    ]
    assert git(root, "merge-base", "--is-ancestor", xs, master) == ""
    origin = git(upstream, "rev-parse", "XS-9.0")
    assert git(root, "log", "-1", "--format=%B", xs).endswith(f"update\n\nImported-from: rpms/foo@{origin}")
    assert git(root, "for-each-ref", "refs/xcraft") == ""
    # a merge brings the package, with its history
    git(root, "merge", "-q", "--no-edit", "import/foo/master")
    assert git(root, "log", "--format=%s", "--", "base/foo/foo.spec").splitlines() == ["update", "update"]

    # the same commits, with the new ones on top, without a git maintenance for each fetch
    commit_files(upstream, {"SPECS/foo.spec": "Name: foo\nVersion: 2.0\nRelease: 2\n"})
    monkeypatch.setenv("GIT_TRACE", str(tmp_path / "trace"))
    assert import_history(root, root / "base/foo", url, ["master"]) == {
        "master": git(root, "rev-parse", "import/foo/master")
    }
    assert git(root, "rev-parse", "import/foo/master~") == master
    assert "git fetch" in (trace := (tmp_path / "trace").read_text())
    assert "git maintenance run" not in trace
    monkeypatch.delenv("GIT_TRACE")
    with pytest.raises(PackageImportError, match="none of the branches to import"):
        import_history(root, root / "base/foo", url, ["XS-10"])
    git(root, "checkout", "-q", "--orphan", "other")
    git(root, "commit", "-qm", "not empty")
    with pytest.raises(PackageImportError, match="must start with an empty commit"):
        import_history(root, root / "base/foo", url, ["master"])


def source_rpm(tmp_path: Path, name: str, version: str) -> Path:
    """Build a source RPM of `name`, with an archive, a patch, and a logo to remove."""
    top = tmp_path / f"rpmbuild-{name}-{version}"
    (top / "SOURCES").mkdir(parents=True)
    (top / f"SOURCES/{name}-{version}.tar.gz").write_text(f"{version}\n")
    (top / "SOURCES/fix.patch").write_text("fix\n")
    (top / "SOURCES/Citrix_Logo_Black.png").write_text("logo\n")
    (top / f"{name}.spec").write_text(
        f"Name: {name}\nVersion: {version}\nRelease: 1\nSummary: {name}\nLicense: MIT\n"
        f"Source0: {name}-{version}.tar.gz\nSource1: Citrix_Logo_Black.png\nPatch0: fix.patch\n%description\n{name}\n"
    )
    # in the build environment: rpmbuild is not on every host
    prepare(Project.find(), roots=False)
    run(Project.find(), "test-srpm", f"rpmbuild -bs -D '_topdir /top' /top/{name}.spec", volumes=[f"{top}:/top"])
    return next((top / "SRPMS").glob("*.src.rpm"))


@pytest.mark.skipif(not shutil.which("podman"), reason="needs podman")
def test_import_srpm(root: Path, upstream: Path, tmp_path: Path) -> None:
    assert runner.invoke(app, ["import", "foo", "--url", f"file://{upstream}"]).exit_code == 0
    before = git(root, "rev-parse", "import/foo/XS-9.0")
    srpm = source_rpm(tmp_path, "foo", "3.0")
    result = runner.invoke(app, ["import", str(srpm)])
    assert result.exit_code == 0, result.output
    commit = git(root, "rev-parse", "import/foo/XS-9.0")
    assert f"foo: import/foo/XS-9.0 at {commit[:12]}" in result.output
    assert git(root, "rev-parse", f"{commit}~") == before
    assert git(root, "ls-tree", "-r", "--name-only", commit).split() == [
        "base/foo/Citrix_Logo_Black.png.deleted-by-XCP-ng.txt", "base/foo/fix.patch", "base/foo/foo.spec",
        "base/foo/sources",
    ]  # fmt: skip
    digest = hashlib.sha256(b"3.0\n").hexdigest()
    assert git(root, "show", f"{commit}:base/foo/sources") == f"{digest}  foo-3.0.tar.gz"
    assert (Project.find().source_cache / digest / "foo-3.0.tar.gz").exists()
    assert git(root, "log", "-1", "--format=%B", commit) == (
        f"Import {srpm.name}\n\nFiles deleted for legal reasons:\n - Citrix_Logo_Black.png\n\n"
        "Signed-off-by: Test User <test@example.com>"
    )
    assert f"import/foo/XS-9.0 has the content of {srpm} already" in runner.invoke(app, ["import", str(srpm)]).output
    # the history of XenServer's packaging repository cannot drop it
    result = runner.invoke(app, ["import", "foo", "--url", f"file://{upstream}"])
    assert "import/foo/XS-9.0 has commits that are not imported" in str(result.exception)
    assert git(root, "rev-parse", "import/foo/XS-9.0") == commit

    # a new package, from the first commit of the project
    assert runner.invoke(app, ["import", str(source_rpm(tmp_path, "bar", "1.0"))]).exit_code == 0
    assert git(root, "rev-parse", "import/bar/XS-9.0~") == git(root, "rev-list", "--max-parents=0", "HEAD")


def test_import_all(root: Path, upstream: Path) -> None:
    assert runner.invoke(app, ["import", "foo", "--url", f"file://{upstream}"]).exit_code == 0
    master = git(root, "rev-parse", "import/foo/master")
    commit_files(upstream, {"SPECS/foo.spec": "Name: foo\nVersion: 3.0\n"})
    # from xcp-ng-rpms by default: a file:// URL can stand for it
    result = runner.invoke(app, ["import", "--all", "--url", f"file://{upstream}"])
    assert result.exit_code == 0, result.output
    assert git(root, "rev-parse", "import/foo/master~") == master


def test_import_options(root: Path, upstream: Path) -> None:
    assert "no package given" in runner.invoke(app, ["import"]).output
    assert "--url applies to a single package" in runner.invoke(app, ["import", "a", "b", "--url", "x"]).output
    assert "not package names: '..'" in runner.invoke(app, ["import", "..", "--url", "x"]).output
    result = runner.invoke(app, ["import", "foo", "--url", f"file://{upstream}", "--repo", "xostor"])
    assert "xostor is not a repository of the project: base" in result.output
    (root / "xcraft.toml").write_text(f'repositories = ["base", "xostor"]\n{(root / "xcraft.toml").read_text()}')
    result = runner.invoke(
        app, ["import", "foo", "--url", f"file://{upstream}", "--repo", "xostor", "--branch", "XS-9.0"]
    )
    assert result.exit_code == 0, result.output
    assert git(root, "ls-tree", "--name-only", "import/foo/XS-9.0") == "xostor"
    assert git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/import") == "import/foo/XS-9.0"


def test_import_refuses_links(root: Path, upstream: Path, tmp_path: Path) -> None:
    # to a file of the host, which the builds would read
    (tmp_path / "secret").write_text("secret")
    (upstream / "SOURCES/key").symlink_to(tmp_path / "secret")
    git(upstream, "add", "-A")
    git(upstream, "commit", "-qm", "link")
    result = runner.invoke(app, ["import", "foo", "--url", f"file://{upstream}"])
    assert "SOURCES/key of " in str(result.exception)
    assert "not a regular file, not imported" in str(result.exception)
    assert git(root, "for-each-ref", "refs/heads/import") == ""
