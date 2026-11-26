"""xcraft reproduce: the packages built again, and compared with their builds."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from tests import commit_base, edit, git, xcraft
from xcraft import build as builder
from xcraft import buildid
from xcraft.cache import build_keys, cached_build
from xcraft.project import Project

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def built(name: str) -> Path:
    """Return the cached build of `name`, of the current key."""
    project = Project.find()
    return cached_build(project, name, build_keys(project)[name])


def should_build_the_packages_again_and_find_them_the_same(root: Path) -> None:
    # its compiler cache, set aside while it is built again, then back
    cached = root / ".xcraft/ccache/trivial/cached"
    cached.parent.mkdir(parents=True)
    cached.write_text("from the builds before")
    code, output = xcraft("reproduce", "trivial")
    assert code == 0, output
    assert "✓ trivial: built again the same" in output
    assert "1 reproducible, 0 not" in output
    assert cached.read_text() == "from the builds before"
    # its build stays, the rebuild is dropped
    assert built("trivial").exists()
    assert not (root / ".xcraft/reproduce/trivial/again").exists()
    assert not (root / ".xcraft/reproduce/trivial/aside").exists()
    # or with its compiler cache, quicker
    code, output = xcraft("reproduce", "trivial", "--with-caches")
    assert code == 0, output
    assert "✓ trivial: built again the same" in output
    assert cached.read_text() == "from the builds before"


def should_keep_the_rebuilds_which_differ_from_their_builds(root: Path) -> None:
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello" > hello.txt', "head -c 16 /dev/urandom > hello.txt"))
    assert xcraft("build", "trivial")[0] == 0
    rpms = sorted(p.read_bytes() for p in (built("trivial") / "RPMS").rglob("*.rpm"))
    code, output = xcraft("reproduce", "trivial")
    assert code == 1, output
    assert "✗ trivial: built again differently, see .xcraft/reproduce/trivial/again" in output
    assert "hello.txt" in output
    assert "0 reproducible, 1 not" in output
    # its build is the one it was, and the rebuild is kept, to see how they differ
    assert sorted(p.read_bytes() for p in (built("trivial") / "RPMS").rglob("*.rpm")) == rpms
    assert list((root / ".xcraft/reproduce/trivial/again/RPMS").rglob("*.rpm"))


def should_build_again_the_builds_reused_from_the_main_branch(root: Path) -> None:
    assert xcraft("build")[0] == 0  # the builds of the main branch
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed")
    # with-deps, built against trivial, built again the same, reuses its build: never made again by xcraft build
    assert xcraft("build")[0] == 0
    assert buildid.read(Project.find())["with-deps"].accepts
    code, output = xcraft("reproduce", "with-deps")
    assert code == 0, output
    assert "✓ with-deps: built again the same" in output
    # still the build of the main branch, which it reuses
    assert buildid.read(Project.find())["with-deps"].accepts
    assert built("with-deps").exists()


def should_tell_a_rebuild_which_failed_from_one_which_differs(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert xcraft("build", "trivial")[0] == 0
    build = builder.build

    def failing(*args: Any, **kwargs: Any) -> tuple[list[builder.Result], dict[str, str], dict[str, str]]:  # noqa: ANN401
        """Fail the rebuild only, the build of xcraft reproduce made again."""
        results, keys, suffixes = build(*args, **kwargs)
        return [builder.Result("trivial", "failed")] if kwargs.get("again") else results, keys, suffixes

    monkeypatch.setattr(builder, "build", failing)
    code, output = xcraft("reproduce", "trivial")
    assert code == 1, output
    assert "✗ trivial: not built again\n  its build failed again" in output
    assert not (root / ".xcraft/reproduce/trivial/again").exists()
