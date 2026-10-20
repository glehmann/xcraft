"""Parts of the builds working on files only: build trees, cleaning, estimates, locking."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

import pytest

from xcraft.build import BuildError, build_keys, prepare_tree, previous_duration, queued
from xcraft.clean import clean, repos_disk_usage
from xcraft.localrepo import RepoError, fresh_dir
from xcraft.project import Config, Project, ProjectLockedError
from xcraft.sources import cache_source, write_manifest
from xcraft.specs import Package, hash_dir, main_spec

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "xcraft.toml").write_text("")
    return Project(tmp_path, Config())


def files(path: Path) -> list[str]:
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


def test_stage_like_a_packaging_repository(project: Project, tmp_path: Path) -> None:
    pkg = project.specs / "foo"
    (pkg / "sub").mkdir(parents=True)
    for name in ("foo.spec", "foo-helper.spec", "fix.patch", "sub/conf", "origin"):
        (pkg / name).write_text(name)
    (tmp_path / "foo-1.0.tar.gz").write_text("archive")
    write_manifest(pkg / "sources", {"foo-1.0.tar.gz": cache_source(project.source_cache, tmp_path / "foo-1.0.tar.gz")})

    tree = prepare_tree(project, "foo")
    # other specs are sources of the main one
    assert files(tree) == ["SOURCES/fix.patch", "SOURCES/foo-1.0.tar.gz", "SOURCES/foo-helper.spec", "SOURCES/sub/conf",
                           "SPECS/foo.spec"]  # fmt: skip
    assert (tree / "SOURCES/foo-1.0.tar.gz").samefile(next(project.source_cache.glob("*/foo-1.0.tar.gz")))

    write_manifest(pkg / "sources", {"foo-2.0.tar.gz": "0" * 64})
    project.packages = {"foo": Package(name="foo", source0="foo-2.0.tar.gz")}  # not a URL to download it from
    with pytest.raises(
        BuildError, match=re.escape("foo-2.0.tar.gz is not in the source cache, run: xcraft import foo")
    ):
        prepare_tree(project, "foo")


def test_main_spec(tmp_path: Path) -> None:
    (tmp_path / "grub").mkdir()
    (tmp_path / "grub/grub2.spec").write_text("")
    assert main_spec(tmp_path / "grub").name == "grub2.spec"
    (tmp_path / "grub/other.spec").write_text("")
    with pytest.raises(RuntimeError, match=re.escape("grub has no grub.spec, and several other specs")):
        main_spec(tmp_path / "grub")


def test_clean_keeps_sources_and_current_builds(project: Project) -> None:
    state = project.state
    for path in ("cache/sources/abc/foo.tar.gz", "cache/builds/foo/current/RPMS/foo.rpm",
                 "cache/builds/foo/old/RPMS/foo.rpm", "cache/builds/gone/old/RPMS/gone.rpm",
                 "build/foo/SPECS/foo.spec", "repo/foo.rpm", "ccache/foo/data"):  # fmt: skip
        (state / path).parent.mkdir(parents=True, exist_ok=True)
        (state / path).write_bytes(b"x" * 8192)

    keys = {"foo": "current", "bar": "current"}
    # only what is of the given packages
    for path in ("cache/builds/bar/current/RPMS/bar.rpm", "cache/builds/bar/old/RPMS/bar.rpm",
                 "build/bar/SPECS/bar.spec", "ccache/bar/data"):  # fmt: skip
        (state / path).parent.mkdir(parents=True, exist_ok=True)
        (state / path).write_bytes(b"x" * 8192)
    assert clean(project, keys, ["bar"], ccache=True) > 0
    assert "cache/builds/bar/current/RPMS/bar.rpm" in files(state)
    assert not [f for f in files(state) if f.startswith(("cache/builds/bar/old", "build/bar", "ccache/bar"))]
    assert {"build/foo/SPECS/foo.spec", "cache/builds/foo/old/RPMS/foo.rpm"} <= set(files(state))
    # what the local repository, the snapshot and the repositories of xcraft repo still link
    rpm = state / "cache/builds/bar/current/RPMS/bar.rpm"
    for link in (state / "repo/bar.rpm", state / "publish/x86_64_v2/os/bar.rpm", project.root / "repo/bar.rpm"):
        link.parent.mkdir(parents=True, exist_ok=True)
        link.hardlink_to(rpm)
    assert repos_disk_usage(project) == 0
    assert clean(project, keys, ["bar"], all_builds=True) == 8192
    assert not [f for f in files(state) if "bar" in f]
    assert repos_disk_usage(project) == 8192  # only repo/ has it now

    assert clean(project, keys) > 0
    assert files(state) == ["cache/builds/foo/current/RPMS/foo.rpm", "cache/sources/abc/foo.tar.gz", "ccache/foo/data"]
    clean(project, keys, all_builds=True, ccache=True)
    assert files(state) == ["cache/sources/abc/foo.tar.gz"]


def test_previous_duration(project: Project) -> None:
    assert previous_duration(project, "foo") is None
    log = project.state / "cache/builds/foo/key/build.log"
    log.parent.mkdir(parents=True)
    log.write_text("Wrote: foo.rpm\n\nreal\t4m13.250s\nuser\t9m1.000s\n")
    assert previous_duration(project, "foo") == 253.25


def test_queued(project: Project) -> None:
    assert queued(project) == []
    queue = project.state / "build/.queue"
    queue.parent.mkdir(parents=True)
    queue.write_text(f"{os.getpid()}\nfoo\nbar\n")
    assert queued(project) == ["foo", "bar"]
    queue.write_text("999999999\nfoo\n")  # its build is gone
    assert queued(project) == []


def test_lock(project: Project) -> None:
    other = Project(project.root, Config())  # as found by another command
    with project.lock():
        with project.lock():  # the command holding it can take it again
            pass
        with pytest.raises(ProjectLockedError, match="another xcraft command is building"), other.lock():
            pass
    with other.lock():  # released
        pass


def test_fresh_dir_only_empties_what_xcraft_made(project: Project, tmp_path: Path) -> None:
    mine = tmp_path / "mine"
    mine.mkdir()
    (mine / "notes.txt").write_text("precious")
    with pytest.raises(RepoError, match="has content xcraft did not make: not emptying it"):
        fresh_dir(project, mine)
    assert (mine / "notes.txt").exists()
    project.state.mkdir()
    with pytest.raises(RepoError, match="has content xcraft did not make"):
        fresh_dir(project, project.state / "../mine")  # in .xcraft/ by its name only
    for path in (tmp_path / "new", project.state / "repo"):  # new, or xcraft's
        fresh_dir(project, path)
        (path / "foo.rpm").write_text("")
        fresh_dir(project, path)  # its marker
        assert files(path) == [".xcraft-output"]
    old = tmp_path / "old"
    (old / "repodata").mkdir(parents=True)
    fresh_dir(project, old)  # a repository from before the marker


def test_keys_of_cycles(project: Project) -> None:
    for name in ("a", "b", "0foo"):
        (project.specs / name).mkdir(parents=True)
        (project.specs / name / f"{name}.spec").write_text(name)
    project.deps = {"a": {"b"}, "b": {"a"}}
    keys = build_keys(project)
    # not the order the cycle is reached in: from a package depending on b, first in name order
    project.deps = {"a": {"b"}, "b": {"a"}, "0foo": {"b"}}
    more = build_keys(project)
    assert (more["a"], more["b"]) == (keys["a"], keys["b"])
    assert keys["a"] != keys["b"]
    (project.specs / "b/b.spec").write_text("b changed")
    project.deps = {"a": {"b"}, "b": {"a"}}
    assert build_keys(project)["a"] != keys["a"]


def test_hash_dir_covers_what_builds_get(tmp_path: Path) -> None:
    pkg = tmp_path / "pkg"
    (pkg / "sub").mkdir(parents=True)
    script = pkg / "script.sh"
    script.write_text("echo")
    (pkg / "origin").write_text("url branch commit")
    before = hash_dir(pkg)
    (pkg / "origin").write_text("another import")  # xcraft's, not the builds'
    assert hash_dir(pkg) == before
    script.chmod(0o600)  # not the modes the umask decides
    assert hash_dir(pkg) == before
    script.chmod(0o755)
    assert hash_dir(pkg) != before
    executable = hash_dir(pkg)
    (pkg / "sub/origin").write_text("a source")
    assert hash_dir(pkg) != executable
    shared = tmp_path / "shared"
    shared.mkdir()
    (pkg / "linked").symlink_to(shared)
    linked = hash_dir(pkg)
    (shared / "file").write_text("in a linked directory")
    assert hash_dir(pkg) != linked
