"""xcraft reproduce: the packages built again, and compared with their builds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests import xcraft
from xcraft.cache import build_keys, cached_build
from xcraft.project import Project

if TYPE_CHECKING:
    from pathlib import Path


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
