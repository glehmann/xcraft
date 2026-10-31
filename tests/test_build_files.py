"""Parts of the builds working on files only: build trees, cleaning, estimates, locking."""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
import typer

from tests import TOML, config, ours
from xcraft import build, buildkey, cache, rpmbuild
from xcraft.build import queued
from xcraft.cache import BuildError, build_keys, previous_duration
from xcraft.checks import _check_made, record_rpm_deps
from xcraft.clean import clean, repos_disk_usage
from xcraft.commands import complete_arch, load
from xcraft.commands.repo import _kept
from xcraft.localrepo import OUTPUT_MARKER, RepoError, fresh_dir, link
from xcraft.lock import Lock, load_lock, save_lock
from xcraft.project import Project, ProjectLockedError
from xcraft.publish import build_dependencies
from xcraft.resolution import Resolved
from xcraft.rpmbuild import prepare_tree
from xcraft.sources import cache_source, write_manifest
from xcraft.specs import STAGED_TIME, Binary, Package, hash_dir, main_spec

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "xcraft.toml").write_text(TOML)
    return Project(tmp_path, config())


def files(path: Path) -> list[str]:
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


def test_stage_like_a_packaging_repository(project: Project, tmp_path: Path) -> None:
    pkg = project.root / "base/foo"
    (pkg / "sub").mkdir(parents=True)
    for name in ("foo.spec", "foo-helper.spec", "fix.patch", "sub/conf", "origin"):
        (pkg / name).write_text(name)
    (tmp_path / "foo-1.0.tar.gz").write_text("archive")
    write_manifest(pkg / "sources", {"foo-1.0.tar.gz": cache_source(project.source_cache, tmp_path / "foo-1.0.tar.gz")})

    (pkg / "fix.patch").chmod(0o700)
    (pkg / "empty").mkdir()
    tree = prepare_tree(project, "foo", hash_dir(pkg, project.root))
    # other specs are sources of the main one
    assert files(tree) == ["SOURCES/fix.patch", "SOURCES/foo-1.0.tar.gz", "SOURCES/foo-helper.spec", "SOURCES/sub/conf",
                           "SPECS/foo.spec"]  # fmt: skip
    assert (tree / "SOURCES/foo-1.0.tar.gz").samefile(next(project.source_cache.glob("*/foo-1.0.tar.gz")))
    # what the hash covers only: the same tree for the same hash, whatever the checkout
    assert not (tree / "SOURCES/empty").exists()
    modes = {path.relative_to(tree).as_posix(): path.stat().st_mode & 0o777 for path in tree.rglob("*")}
    assert modes == {"SOURCES": 0o755, "SOURCES/fix.patch": 0o755, "SOURCES/foo-1.0.tar.gz": 0o444,
                     "SOURCES/foo-helper.spec": 0o644, "SOURCES/sub": 0o755, "SOURCES/sub/conf": 0o644,
                     "SPECS": 0o755, "SPECS/foo.spec": 0o644}  # fmt: skip
    assert {path.stat().st_mtime for path in [tree, *tree.rglob("*")]} == {STAGED_TIME}
    # nor another one than the one of the key
    with pytest.raises(BuildError, match="base/foo changed since the builds started"):
        prepare_tree(project, "foo", "0" * 64)

    write_manifest(pkg / "sources", {"foo-2.0.tar.gz": "0" * 64})
    project.packages = {"foo": Package(name="foo", source0="foo-2.0.tar.gz")}  # not a URL to download it from
    with pytest.raises(
        BuildError,
        match=re.escape("foo-2.0.tar.gz is not in the source cache, the remote storage, nor at a URL of the spec"),
    ):
        prepare_tree(project, "foo")


def test_stage_whatever_the_umask(project: Project) -> None:
    pkg = project.root / "base/foo"
    (pkg / "sub").mkdir(parents=True)
    for name in ("foo.spec", "sub/conf"):
        (pkg / name).write_text(name)

    def staged(umask: int) -> dict[str, tuple[int, float, bytes]]:
        previous = os.umask(umask)
        try:
            tree = prepare_tree(project, "foo", hash_dir(pkg, project.root))
        finally:
            os.umask(previous)
        return {
            p.relative_to(tree).as_posix(): (
                p.stat().st_mode,
                p.stat().st_mtime,
                p.read_bytes() if p.is_file() else b"",
            )
            for p in [tree, *tree.rglob("*")]
        }

    assert staged(0o077) == staged(0o022)


def test_stage_archives_of_the_manifest_hashed(
    project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = project.root / "base/foo"
    pkg.mkdir(parents=True)
    (pkg / "foo.spec").write_text("foo")
    digests = {}
    for version in ("1", "2"):
        (archive := tmp_path / f"foo-{version}.tar.gz").write_text(version)
        digests[archive.name] = cache_source(project.source_cache, archive)
    write_manifest(pkg / "sources", {"foo-1.tar.gz": digests["foo-1.tar.gz"]})
    hashed = hash_dir(pkg, project.root)
    fetch = rpmbuild.fetch_sources

    def edited(*args: object) -> list[str]:  # the manifest edited once hashed, and restored after
        write_manifest(pkg / "sources", {"foo-2.tar.gz": digests["foo-2.tar.gz"]})
        return fetch(*args)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(rpmbuild, "fetch_sources", edited)
    tree = prepare_tree(project, "foo", hashed)
    write_manifest(pkg / "sources", {"foo-1.tar.gz": digests["foo-1.tar.gz"]})
    assert hash_dir(pkg, project.root) == hashed
    # the archive of the manifest hashed, not of the one read after
    assert sorted(p.name for p in (tree / "SOURCES").iterdir()) == ["foo-1.tar.gz"]


def test_link_replaces(tmp_path: Path) -> None:
    # the builds of two keys, with packages of the same name, in a repository
    first, second, repo = tmp_path / "first.rpm", tmp_path / "second.rpm", tmp_path / "repo.rpm"
    first.write_text("first")
    second.write_text("second")
    link(first, repo)
    link(second, repo)
    assert repo.read_text() == "second"
    assert first.read_text() == "first"  # in the cache, as built
    link(second, repo)  # again
    assert repo.samefile(second)


def test_main_spec(tmp_path: Path) -> None:
    (tmp_path / "grub").mkdir()
    (tmp_path / "grub/grub2.spec").write_text("")
    assert main_spec(tmp_path / "grub").name == "grub2.spec"
    (tmp_path / "grub/other.spec").write_text("")
    with pytest.raises(RuntimeError, match=re.escape("grub has no grub.spec, and several other specs")):
        main_spec(tmp_path / "grub")


def test_clean_keeps_sources_and_current_builds(project: Project) -> None:
    state = project.state
    for path in ("cache/sources/abc/foo.tar.gz", "cache/builds/x86_64_v2/foo/current/RPMS/foo.rpm",
                 "cache/builds/x86_64_v2/foo/old/RPMS/foo.rpm", "cache/builds/x86_64_v2/gone/old/RPMS/gone.rpm",
                 "build/foo/SPECS/foo.spec", "repo/foo.rpm", "ccache/foo/data"):  # fmt: skip
        (state / path).parent.mkdir(parents=True, exist_ok=True)
        (state / path).write_bytes(b"x" * 8192)

    keys = {"foo": "current", "bar": "current"}
    # only what is of the given packages
    for path in ("cache/builds/x86_64_v2/bar/current/RPMS/bar.rpm", "cache/builds/x86_64_v2/bar/old/RPMS/bar.rpm",
                 "build/bar/SPECS/bar.spec", "ccache/bar/data"):  # fmt: skip
        (state / path).parent.mkdir(parents=True, exist_ok=True)
        (state / path).write_bytes(b"x" * 8192)
    assert clean(project, [keys], ["bar"], ccache=True) > 0
    assert "cache/builds/x86_64_v2/bar/current/RPMS/bar.rpm" in files(state)
    assert not [f for f in files(state) if f.startswith(("cache/builds/x86_64_v2/bar/old", "build/bar", "ccache/bar"))]
    assert {"build/foo/SPECS/foo.spec", "cache/builds/x86_64_v2/foo/old/RPMS/foo.rpm"} <= set(files(state))
    # what the local repository, the snapshot and the repositories of xcraft repo still link
    rpm = state / "cache/builds/x86_64_v2/bar/current/RPMS/bar.rpm"
    for linked in (state / "repo/bar.rpm", state / "publish/base/x86_64_v2/bar.rpm", project.root / "repo/bar.rpm"):
        linked.parent.mkdir(parents=True, exist_ok=True)
        linked.hardlink_to(rpm)
    assert repos_disk_usage(project) == 0
    assert clean(project, [keys], ["bar"], all_builds=True) == 8192
    assert not [f for f in files(state) if "bar" in f]
    assert repos_disk_usage(project) == 0  # of a directory xcraft repo did not make
    (project.root / "repo" / OUTPUT_MARKER).touch()
    assert repos_disk_usage(project) == 8192  # only repo/ has it now

    assert clean(project, [keys]) > 0
    assert files(state) == [
        "cache/builds/x86_64_v2/foo/current/RPMS/foo.rpm",
        "cache/sources/abc/foo.tar.gz",
        "ccache/foo/data",
    ]
    clean(project, [keys], all_builds=True, ccache=True)
    assert files(state) == ["cache/sources/abc/foo.tar.gz"]


def test_previous_duration(project: Project) -> None:
    assert previous_duration(project, "foo") is None
    log = project.state / "cache/builds/x86_64_v2/foo/key/build.log"
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
    # the command holding it can take it again, also through the project on another platform
    with project.lock(), project.lock(), project.all_platforms()[1].lock():
        pass
    project.state.mkdir(exist_ok=True)
    with (project.state / "lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # as another command does
        with pytest.raises(ProjectLockedError, match="another xcraft command is building"), project.lock():
            pass
    with project.lock():  # released
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


def test_package_dirs(project: Project) -> None:
    project.config.repositories = ["base", "xostor"]
    for path in ("base/foo", "base/.foo.import", "xostor/bar", "iso/bar"):
        (project.root / path).mkdir(parents=True)
    assert project.package_dirs == {"bar": project.root / "xostor/bar", "foo": project.root / "base/foo"}
    (project.root / "xostor/foo").mkdir()
    with pytest.raises(RuntimeError, match="foo is in two repositories: base and xostor"):
        _ = Project(project.root, project.config).package_dirs


def test_find(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "project/base/foo").mkdir(parents=True)
    (tmp_path / "project/xcraft.toml").write_text(TOML)
    monkeypatch.delenv("XCRAFT_PROJECT", raising=False)
    monkeypatch.chdir(tmp_path / "project/base/foo")
    assert Project.find().root == tmp_path / "project"
    monkeypatch.chdir(tmp_path)
    with pytest.raises(RuntimeError, match=r"no xcraft.toml in .* or its parents"):
        Project.find()
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    with pytest.raises(RuntimeError, match=r"no xcraft.toml in \S+$"):
        Project.find()


@pytest.mark.parametrize("location", ["outside", "cwd", "override", "missing"])
def test_find_from_executable(tmp_path: Path, location: str) -> None:
    executable = tmp_path / "installed/.venv/bin/xcraft"
    executable.parent.mkdir(parents=True)
    executable.write_text("from xcraft.project import Project\nprint(Project.find().root)\n")
    for name in ("installed", "cwd", "override"):
        (tmp_path / name).mkdir(exist_ok=True)
        (tmp_path / name / "xcraft.toml").write_text(TOML)
    env = {k: v for k, v in os.environ.items() if k != "XCRAFT_PROJECT"}
    if location in {"override", "missing"}:
        env["XCRAFT_PROJECT"] = str(tmp_path / location)
    result = subprocess.run(
        [sys.executable, str(executable)],
        cwd=tmp_path / "cwd" if location == "cwd" else tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if location == "missing":
        assert result.returncode != 0
        assert f"no xcraft.toml in {tmp_path / 'missing'}" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == str(tmp_path / ("installed" if location == "outside" else location))


def test_platforms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    monkeypatch.delenv("XCRAFT_ARCH", raising=False)
    for machine, arch in (("x86_64", "x86_64_v2"), ("aarch64", "aarch64"), ("ppc64le", "x86_64_v2")):
        monkeypatch.setattr(os, "uname", lambda machine=machine: os.uname_result(("Linux", "h", "r", "v", machine)))
        assert Project.find().arch == arch  # the host's, or the first one
    monkeypatch.setenv("XCRAFT_ARCH", "x86_64_v2")
    assert Project.find().arch == "x86_64_v2"
    assert Project.find("aarch64").arch == "aarch64"
    with pytest.raises(RuntimeError, match="x86_64 is not an architecture of the project: x86_64_v2, aarch64"):
        Project.find("x86_64")
    assert (complete_arch(""), complete_arch("a")) == (["x86_64_v2", "aarch64"], ["aarch64"])
    x86, arm = Project.find().all_platforms()
    assert ("epel" in x86.repos, "epel-fedora" in x86.repos) == (True, False)
    assert ("epel" in arm.repos, "epel-fedora" in arm.repos) == (False, True)


def test_build_dependencies(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = Binary(name="kernel-headers", evr="6.12-1", arch="x86_64_v2", provides=[], requires=[])
    project.packages = {"kernel": Package(name="kernel", binaries=[binary])}
    resolved = Resolved(image=["bash", "gcc", "kernel-headers"], buildroots={"xen": ["zlib-devel", "gcc"]})
    monkeypatch.setattr("xcraft.publish.resolve", lambda _: resolved)
    save_lock(project, Lock(runtime={project.arch: ["bash"]}))
    assert build_dependencies(project) == ["gcc", "zlib-devel"]  # but ours, and those of the base repository


def test_check_made(project: Project, tmp_path: Path) -> None:
    binary = Binary(name="foo", evr="1.0-1", arch="x86_64", provides=["foo = 1.0-1"], requires=["bar >= 2"])
    debug = Binary(name="foo-debuginfo", evr="1.0-1", arch="x86_64", provides=[], requires=[])
    project.parsed = {"foo": Package(name="foo", binaries=[binary, debug])}
    made = {
        "foo": {"evr": "1.0-1", "arch": "x86_64", "provides": ["foo = 1.0-1", "libfoo.so.1()(64bit)"],
                "requires": ["bar >= 2", "libc.so.6()(64bit)"]},
        # the debug packages rpm splits while building
        "foo-tools-debuginfo": {"evr": "1.0-1", "arch": "x86_64", "provides": [], "requires": []},
    }  # fmt: skip
    (tmp_path / "made.json").write_text(json.dumps(made))
    _check_made(project, "foo", tmp_path, suffix="")
    made["foo"] |= {"evr": "1.0-1.0.fix.1", "requires": ["libc.so.6()(64bit)"]}
    (tmp_path / "made.json").write_text(json.dumps({**made, "foo-extra": made["foo"]}))
    problems = (
        r"\+foo-extra; foo-1\.0-1\.0\.fix\.1\.x86_64 rather than 1\.0-1\.x86_64; foo without the requires bar >= 2"
    )
    with pytest.raises(BuildError, match=f": {problems}: xcraft parses"):
        _check_made(project, "foo", tmp_path, suffix="")
    # the release suffix, %autorev, at the start of %dist, wherever the spec has it
    numbered = binary.model_copy(update={"evr": "1.0-1.xcpng", "provides": ["foo = 1.0-1.xcpng"]})
    project.parsed = {"foo": Package(name="foo", dist=".xcpng", binaries=[numbered, debug])}
    made["foo"] |= {"evr": "1.0-1+b2.xcpng", "provides": ["foo = 1.0-1+b2.xcpng"], "requires": ["bar >= 2"]}
    (tmp_path / "made.json").write_text(json.dumps(made))
    _check_made(project, "foo", tmp_path, suffix="+b2")
    with pytest.raises(BuildError, match=r"foo-1\.0-1\+b2\.xcpng\.x86_64 rather than 1\.0-1\+b3\.xcpng\.x86_64"):
        _check_made(project, "foo", tmp_path, suffix="+b3")
    # or at the end of the release, for the packages without %dist, like the one defining it
    numbered = binary.model_copy(update={"evr": "1.0-2.abc", "provides": ["foo = 1.0-2.abc"]})
    project.parsed = {"foo": Package(name="foo", dist=".xcpng", binaries=[numbered, debug])}
    made["foo"] |= {"evr": "1.0-2.abc+b2", "provides": ["foo = 1.0-2.abc+b2"]}
    (tmp_path / "made.json").write_text(json.dumps(made))
    _check_made(project, "foo", tmp_path, suffix="+b2")
    # in the names including the release too, like the livepatch packages of the kernel
    for release, built in ((".xcpng", "+b2.xcpng"), ("", "+b2")):
        named = numbered.model_copy(
            update={
                "name": f"foo-lp_1.0_2.abc{release}",
                "autorev_name": f"foo-lp_1.0_2.abc%{{autorev}}{release}",  # as parsed
                "evr": f"1.0-2.abc{release}",
                "provides": [],
            }
        )
        project.parsed = {"foo": Package(name="foo", dist=".xcpng", binaries=[named])}
        lp = {f"foo-lp_1.0_2.abc{built}": made["foo"] | {"evr": f"1.0-2.abc{built}", "provides": []}}
        (tmp_path / "made.json").write_text(json.dumps(lp))
        _check_made(project, "foo", tmp_path, suffix="+b2")


def test_record_rpm_deps(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    build = project.state / "cache/builds/x86_64_v2/foo/key"
    (build / "RPMS/x86_64").mkdir(parents=True)
    (build / "RPMS/x86_64/foo-1.0-1.x86_64.rpm").touch()
    binary = Binary(name="foo", evr="1.0-1", arch="x86_64", provides=[], requires=[])
    project.parsed = {"foo": Package(name="foo", binaries=[binary])}
    # what cannot be recorded yet is not kept, to record it again next time
    monkeypatch.setattr(
        "xcraft.checks.resolve_rpm_deps", lambda *_: Resolved(errors={"foo": "nothing provides libbar.so.1"})
    )
    assert not record_rpm_deps(project, ["foo"], {"foo": "key"})
    assert not (build / "rpm-deps.json").exists()
    monkeypatch.setattr("xcraft.checks.resolve_rpm_deps", lambda *_: Resolved(requires={"foo": ["bar"]}))
    assert record_rpm_deps(project, ["foo"], {"foo": "key"})
    assert load_lock(project).requires.shared == {"foo": ["bar"]}
    monkeypatch.setattr("xcraft.checks.resolve_rpm_deps", lambda *_: pytest.fail("recorded again"))
    assert not record_rpm_deps(project, ["foo"], {"foo": "key"})
    # but with a build of ours, which it may resolve to instead: a new one, or another of the same package
    baz = Binary(name="baz", evr="1.0-1", arch="x86_64", provides=[], requires=[])
    project.parsed = {"foo": Package(name="foo", binaries=[binary]), "baz": Package(name="baz", binaries=[baz])}
    for key, provider in (("first", "baz"), ("second", "bar")):
        (rpms := project.state / f"cache/builds/x86_64_v2/baz/{key}/RPMS/x86_64").mkdir(parents=True)
        (rpms / "baz-1.0-1.x86_64.rpm").touch()
        monkeypatch.setattr(
            "xcraft.checks.resolve_rpm_deps", lambda *_, provider=provider: Resolved(requires={"foo": [provider]})
        )
        assert record_rpm_deps(project, ["foo"], {"foo": "key", "baz": key})
        assert load_lock(project).requires.shared == {"foo": [provider]}


def test_record_providers_named_with_their_releases(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    # foo needs baz-lp, a package of ours named with its release, like the livepatch packages of the kernel
    keys = {"foo": "1" * 64 + "+b1", "baz": "2" * 64 + "+b3"}
    for name, rpm in (("foo", "foo-1.0-1+b1.x86_64.rpm"), ("baz", "baz-lp_1_1+b3-1.0-1+b3.x86_64.rpm")):
        (build := project.state / f"cache/builds/x86_64_v2/{name}/{keys[name]}/RPMS/x86_64").mkdir(parents=True)
        (build / rpm).touch()
    foo = Binary(name="foo", evr="1.0-1", arch="x86_64", provides=[], requires=[])
    lp = Binary(name="baz-lp_1_1", autorev_name="baz-lp_1_1%{autorev}", evr="1.0-1", arch="x86_64", provides=[],
                requires=[])  # fmt: skip
    project.parsed = {"foo": Package(name="foo", binaries=[foo]), "baz": Package(name="baz", binaries=[lp])}
    monkeypatch.setattr("xcraft.checks.resolve_rpm_deps", lambda *_: Resolved(requires={"foo": ["baz-lp_1_1+b3"]}))
    assert record_rpm_deps(project, ["foo"], keys)
    # by its name in the spec, whatever its build
    assert load_lock(project).requires.shared == {"foo": ["baz-lp_1_1"]}


def test_builds_still_to_build_again_fail(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    project.deps = {"a": set()}
    monkeypatch.setattr(build, "prepare", lambda _: None)
    monkeypatch.setattr(build, "build_image", lambda _: "image")
    monkeypatch.setattr(build, "_record_cached", lambda *_: ({"a": "key"}, {}, {}))
    monkeypatch.setattr(build, "update_build_ids", lambda _: None)
    attempts = []
    monkeypatch.setattr(
        build, "_build_all", lambda *_, **__: attempts.append(1) or ([build.Result("a", "again")], {"a": "key"}, {})
    )
    assert build.build(project, ["a"]) == ([build.Result("a", "failed")], {"a": "key"}, {})
    assert len(attempts) == 2


def test_packages_edited_while_building(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "record_rpm_deps", lambda *_: False)
    for name in ("a", "b", "c"):
        (project.root / "base" / name).mkdir(parents=True)
        (project.root / "base" / name / f"{name}.spec").write_text(name)
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ours(project, monkeypatch, {"a": set(), "b": {"a"}, "c": set()})
    hashes = buildkey.directories(project)
    keys = build_keys(project, hashes=hashes)
    (cache.cached_build(project, "b", keys["b"])).mkdir(parents=True)

    def building(_project: Project, name: str, *_: object, **__: object) -> build.Result:
        if name == "c":  # a, which b depends on, edited meanwhile
            (project.root / "base/a/a.spec").write_text("a edited")
        cache.cached_build(project, name, keys[name]).mkdir(parents=True)
        return build.Result(name, "built")

    monkeypatch.setattr(build, "_build_shown", building)
    (project.state / "build").mkdir(parents=True)  # for the queue, which build() makes
    results, _, _ = build._build_all(
        project, ["a", "c", "b"], keys, hashes, verbose=False, dev=None, autorev={}, fetch="none", build_missing=True
    )
    # the builds work on the packages as they were at the start: b fails, rather than b cached with the old a
    assert results == [build.Result("a", "built"), build.Result("c", "built"), build.Result("b", "failed")]


def test_keys_changed_by_the_last_recording(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a", "b"):
        (project.root / "base" / name).mkdir(parents=True)
        (project.root / "base" / name / f"{name}.spec").write_text(name)
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ours(project, monkeypatch, {"a": set(), "b": {"a"}})
    monkeypatch.setattr(build, "prepare", lambda _: None)
    hashes = buildkey.directories(project)
    keys = build_keys(project, hashes=hashes)
    for name in ("a", "b"):
        cache.cached_build(project, name, keys[name]).mkdir(parents=True)

    def record(*_: object, quiet: bool = False) -> bool:
        # the last one, of the cached builds, finds what changes the build root of b
        if quiet:
            return False
        lock = load_lock(project)
        lock.buildrequires.shared["b"] = ["found"]
        save_lock(project, lock)
        return True

    monkeypatch.setattr(build, "record_rpm_deps", record)
    (project.state / "build").mkdir(parents=True)  # for the queue, which build() makes
    results, after, _ = build._build_all(
        project, ["a", "b"], keys, hashes, verbose=False, dev=None, autorev={}, fetch="none", build_missing=True
    )
    # b to build again, with its new key, not cached
    assert results == [build.Result("a", "cached"), build.Result("b", "again")]
    assert (after["a"], after["b"]) == (keys["a"], after["b"])
    assert after["b"] != keys["b"]


def test_keys_changed_during_the_builds(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a", "b", "c"):
        (project.root / "base" / name).mkdir(parents=True)
        (project.root / "base" / name / f"{name}.spec").write_text(name)
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ours(project, monkeypatch, {"a": set(), "b": {"a"}, "c": set()})
    monkeypatch.setattr(build, "prepare", lambda _: None)
    hashes = buildkey.directories(project)
    keys = build_keys(project, hashes=hashes)
    for name in ("a", "b", "c"):
        cache.cached_build(project, name, keys[name]).mkdir(parents=True)
    monkeypatch.setattr(build, "record_rpm_deps", lambda *_, **__: False)  # the last recording changes nothing
    done = build._done

    def changing(project: Project, name: str, *args: object, **kwargs: object) -> build.Result | None:
        if name == "c":  # what changes the build root of b, found after it, like by another command
            lock = load_lock(project)
            lock.buildrequires.shared["b"] = ["found"]
            save_lock(project, lock)
        return done(project, name, *args, **kwargs)  # ty: ignore[invalid-argument-type]

    monkeypatch.setattr(build, "_done", changing)
    (project.state / "build").mkdir(parents=True)  # for the queue, which build() makes
    results, after, _ = build._build_all(
        project, ["a", "b", "c"], keys, hashes, verbose=False, dev=None, autorev={}, fetch="none", build_missing=True
    )
    # b to build again, with its new key, not cached
    assert results == [build.Result("a", "cached"), build.Result("b", "again"), build.Result("c", "cached")]
    assert after["b"] != keys["b"]


def test_stored_builds_of_keys_changed(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "record_rpm_deps", lambda *_, **__: False)
    keys = {"a": "1" * 64, "b": "2" * 64}
    stored = {cache.build_path(project, "a", keys["a"]): True, cache.build_path(project, "b", "3" * 64): True}
    results = {name: build.Result(name, "stored") for name in keys}
    # b in the storage with the key it had then, not with the final one: fetched or built again
    after = build._recorded_again(project, results, keys, {}, b"", stored, dev=None, hashes={})[0]
    assert after == [build.Result("a", "stored"), build.Result("b", "again")]


def test_recordings_not_converging(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "dev_keys", lambda *_: ({}, {}))
    monkeypatch.setattr(build, "record_rpm_deps", lambda *_, **__: True)
    monkeypatch.setattr(build, "prepare", lambda _: None)
    with pytest.raises(BuildError, match="keeps changing the keys"):
        build._record_cached(project, None)


def test_unknown_packages_before_preparing(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XCRAFT_PROJECT", str(project.root))
    monkeypatch.setattr("xcraft.commands.prepare", lambda _: pytest.fail("prepared"))
    with pytest.raises(typer.BadParameter, match="unknown package"):
        load(["nope"])


def test_packages_kept_by_test_hosts() -> None:
    rpms = ["xen-4.17-1+b3~2.dev.x86_64.rpm", "xen-libs-4.17-1+b3~2.dev.x86_64.rpm", "qemu-9-1+b5~1.dev.x86_64.rpm"]
    installed = [
        "xen-4.17-1+b3~2.dev.x86_64",  # upgraded
        "xen-libs-4.17-1+b3~4.qcow2.x86_64",  # newer, from another identifier
        "package qemu is not installed",
    ]
    assert _kept(installed, rpms) == [("xen-libs-4.17-1+b3~4.qcow2.x86_64", "xen-libs-4.17-1+b3~2.dev.x86_64")]
