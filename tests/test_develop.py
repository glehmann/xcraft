"""Packages built from the git trees of their projects: xcraft develop."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests import copy_project
from tests.test_buildid import commit_base, git
from xcraft.__main__ import app
from xcraft.develop import ARCHIVE, DevelopError, spec_text

runner = CliRunner()

SPEC = """\
Name: trivial
Version: 1.0
Release: 1%{?dist}
Summary: Trivial test package, from its sources
License: MIT
BuildArch: noarch
Source0: trivial-1.0.tar.gz
Patch0: 0001-fix.patch

%description
Trivial package, built from its sources.

%prep
%autosetup -p1

%build
# what the previous build left: the directory of the sources is kept
test -e built-before && cp built-before kept.txt || echo first > kept.txt
touch built-before
cp hello.in hello.txt

%install
mkdir -p %{buildroot}/usr/share/%{name}
cp hello.txt kept.txt %{buildroot}/usr/share/%{name}/

%files
/usr/share/%{name}

%changelog
* Mon Jul 10 2026 Test User <test@example.com> - 1.0-1
- Initial package
"""


def test_spec_of_a_tree() -> None:
    spec = spec_text(SPEC.replace("%autosetup -p1", "%setup -q\n%patch -P0 -p1\n%autopatch -p1"))
    assert f"Source0: {ARCHIVE}\n" in spec
    assert "Patch" not in spec
    assert "%patch" not in spec
    assert "%autopatch" not in spec
    assert "%setup -q -T -D -n %{name}-%{version}\npython3 /xcraft/develop.py %{SOURCE0}\n" in spec
    assert "cp hello.in hello.txt" in spec  # the rest as it is
    # in the directory of the builds of the package, for the cache of the compilers
    assert "%setup -q -T -D -n linux-%{usver}\n" in spec_text(SPEC.replace("-p1", "-p1 -n linux-%{usver}"))
    assert "%setup -q -T -D -n src\n" in spec_text(SPEC.replace("%autosetup -p1", "%setup -qn src"))
    # without %setup: nothing to replace with the tree
    with pytest.raises(DevelopError, match="no %setup nor %autosetup in its %prep"):
        spec_text(SPEC.replace("%autosetup -p1\n", ""))


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_environment: Path) -> Path:
    """Return the project, on a branch of its main one, with trivial built from its sources."""
    root = tmp_path / "project"
    copy_project(root, build_environment)
    (root / "base/trivial/trivial.spec").write_text(SPEC)
    (root / "base/trivial/0001-fix.patch").write_text("not applied: the tree has its patches\n")
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    return root


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """Return the git tree of the project of trivial, at its tag."""
    tree = tmp_path / "trivial"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    (tree / "hello.in").write_text("hello from the tree\n")
    (tree / "unchanged.in").write_text("unchanged\n")
    git(tree, "add", ".")
    git(tree, "commit", "-qm", "first")
    git(tree, "tag", "v1.0")
    return tree


def xcraft(*args: str) -> tuple[int, str]:
    result = runner.invoke(app, list(args))
    error = result.exception if result.exception and not isinstance(result.exception, SystemExit) else ""
    return result.exit_code, f"{result.output}{error}"


def test_develop_from_a_tree(root: Path, tree: Path) -> None:
    builds = (root / "xcraft.builds").read_text()
    code, output = xcraft("develop", str(tree))
    assert code == 0, output
    assert f"trivial: developed from {tree}" in output
    code, output = xcraft("develop")
    assert code == 0, output
    assert f"trivial ← {tree} v1.0" in output
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    sources = root / ".xcraft/build/trivial/BUILD/trivial-1.0"
    assert (sources / "hello.txt").read_text() == "hello from the tree\n"
    assert (sources / "kept.txt").read_text() == "first\n"
    unchanged = (sources / "unchanged.in").stat().st_mtime_ns
    # its files as they are, committed or not, untracked ones too, but those git ignores, in the directory kept
    (tree / "hello.in").write_text("hello, edited\n")
    (tree / "new.in").write_text("new\n")
    (tree / ".gitignore").write_text("*.o\n")
    (tree / "ignored.o").write_text("ignored\n")
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert "✓ trivial built" in output
    assert (sources / "hello.txt").read_text() == "hello, edited\n"
    assert (sources / "kept.txt").exists()
    assert (sources / "kept.txt").read_text() == ""  # from built-before, of the previous build
    assert (sources / "new.in").exists()
    assert not (sources / "ignored.o").exists()
    assert (sources / "unchanged.in").stat().st_mtime_ns == unchanged
    # the same files: cached
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert "✓ trivial built" not in output
    # removed from the tree, from the directory too
    (tree / "new.in").unlink()
    assert xcraft("build", "trivial")[0] == 0
    assert not (sources / "new.in").exists()
    # never in the build IDs, which are of git
    assert (root / "xcraft.builds").read_text() == builds
    # nor in the final builds
    code, output = xcraft("build", "--final", "trivial")
    assert code == 1
    assert "final builds with packages developed from trees, trivial: xcraft develop --stop first" in output
    code, output = xcraft("develop", "--stop")
    assert code == 0, output
    assert "stopped developing trivial" in output
    assert xcraft("develop")[1].endswith("no package developed from a tree\n")


def test_develop_errors(root: Path, tmp_path: Path) -> None:
    code, output = xcraft("develop", str(tmp_path))
    assert code == 2
    assert "is not a git tree" in re.sub(r"[│\s]+", " ", output)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    code, output = xcraft("develop", str(other))
    assert code == 2
    assert "no package matches the repository other, give it with --package" in re.sub(r"[│\s]+", " ", output)
    code, output = xcraft("develop", "--stop", "trivial")
    assert code == 2
    assert "not developed from a tree: trivial" in re.sub(r"[│\s]+", " ", output)
