"""Parts of the builds working on files only: build trees, cleaning, estimates, locking."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest

from tests import TOML, config, ours
from xcraft import build, buildkey, cache, localrepo, resolution, rpmbuild, specs
from xcraft.build import queued
from xcraft.builds import load_builds, save_builds
from xcraft.cache import BuildError, build_keys, previous_duration
from xcraft.checks import _check_made, record_rpm_deps
from xcraft.clean import caches_removal, freed, remove, repos_disk_usage, routine_removal, sources_removal
from xcraft.localrepo import OUTPUT_MARKER, RepoError, extra_rpms, fresh_dir
from xcraft.lock import Lock, Pin, save_lock
from xcraft.packages import Binary, Package
from xcraft.project import Extra, Project
from xcraft.publish import build_dependencies
from xcraft.resolution import Resolved
from xcraft.rpmbuild import prepare_tree
from xcraft.sources import SourceError, cache_source, parse_manifest, write_manifest
from xcraft.specs import STAGED_TIME, hash_dir, main_spec


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "xcraft.toml").write_text(TOML)
    return Project(tmp_path, config())


def files(path: Path) -> list[str]:
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


def should_stage_the_build_tree_like_a_packaging_repository(project: Project, tmp_path: Path) -> None:
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
    project.parsed = {"foo": Package(name="foo", source0="foo-2.0.tar.gz")}  # not a URL to download it from
    with pytest.raises(
        BuildError,
        match=re.escape("foo-2.0.tar.gz is not in the source cache, the remote storage, nor at a URL of the spec"),
    ):
        prepare_tree(project, "foo")


@pytest.mark.parametrize("name", ["{outside}", "../../../outside", "sub/../x", ".."])
def should_refuse_manifest_archives_outside_the_cache(name: str, project: Project, tmp_path: Path) -> None:
    """A manifest naming a file out of the cache, of the right SHA-256, leaves it as it is."""
    pkg = project.root / "base/foo"
    pkg.mkdir(parents=True)
    (pkg / "foo.spec").write_text("foo")
    (outside := tmp_path / "outside").write_text("known")
    (pkg / "sources").write_text(f"{hashlib.sha256(b'known').hexdigest()}  {name.format(outside=outside)}\n")
    with pytest.raises(SourceError, match="invalid sources manifest"):
        prepare_tree(project, "foo")
    assert outside.read_text() == "known"
    assert outside.stat().st_mode & 0o777 != 0o444


def should_parse_the_lines_of_a_sources_manifest() -> None:
    digest = "a" * 64
    assert parse_manifest(f"{digest}  foo.tar.gz\n\n  \n{digest}\tfoo bar.tgz \n") == {
        "foo.tar.gz": digest,
        "foo bar.tgz": digest,
    }
    for text in (f"{digest.upper()}  foo", f"{digest}", "abc  foo", f"{digest}  foo\n{digest}  foo"):
        with pytest.raises(SourceError):
            parse_manifest(text)


def should_refuse_to_link_a_file_to_itself(tmp_path: Path) -> None:
    (file := tmp_path / "file").write_text("kept")
    with pytest.raises(RepoError, match="to itself"):
        localrepo.link(file, tmp_path / "." / "file")
    assert file.read_text() == "kept"


def should_stage_the_same_tree_whatever_the_umask(project: Project) -> None:
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


def should_stage_the_archives_of_the_manifest_as_hashed(
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

    # the manifest edited once hashed, and restored after
    def edited(project: Project, name: str, manifest: dict[str, str] | None = None) -> list[str]:
        write_manifest(pkg / "sources", {"foo-2.tar.gz": digests["foo-2.tar.gz"]})
        return fetch(project, name, manifest)

    monkeypatch.setattr(rpmbuild, "fetch_sources", edited)
    tree = prepare_tree(project, "foo", hashed)
    write_manifest(pkg / "sources", {"foo-1.tar.gz": digests["foo-1.tar.gz"]})
    assert hash_dir(pkg, project.root) == hashed
    # the archive of the manifest hashed, not of the one read after
    assert sorted(p.name for p in (tree / "SOURCES").iterdir()) == ["foo-1.tar.gz"]


def should_download_extra_upstream_packages_as_pinned(project: Project, tmp_path: Path) -> None:
    (served := tmp_path / "foo-1.0-1.noarch.rpm").write_bytes(b"pinned")
    pinned = hashlib.sha256(b"pinned").hexdigest()
    project.config.extra_upstream = {"foo": Extra(url=served.as_uri(), sha256=pinned)}
    [rpm] = extra_rpms(project)
    assert rpm.read_bytes() == b"pinned"
    # damaged in the cache: downloaded again
    rpm.write_bytes(b"damaged")
    assert extra_rpms(project)[0].read_bytes() == b"pinned"
    # of other contents at its URL: refused
    served.write_bytes(b"other")
    rpm.unlink()
    with pytest.raises(RepoError, match=f"has other contents, {hashlib.sha256(b'other').hexdigest()}, than pinned"):
        extra_rpms(project)


def should_replace_a_package_linked_in_a_repository(tmp_path: Path) -> None:
    # the builds of two keys, with packages of the same name, in a repository
    first, second, repo = tmp_path / "first.rpm", tmp_path / "second.rpm", tmp_path / "repo.rpm"
    first.write_text("first")
    second.write_text("second")
    localrepo.link(first, repo)
    localrepo.link(second, repo)
    assert repo.read_text() == "second"
    assert first.read_text() == "first"  # in the cache, as built
    localrepo.link(second, repo)  # again
    assert repo.samefile(second)


def should_find_the_main_spec_of_a_package(tmp_path: Path) -> None:
    (tmp_path / "grub").mkdir()
    (tmp_path / "grub/grub2.spec").write_text("")
    assert main_spec(tmp_path / "grub").name == "grub2.spec"
    (tmp_path / "grub/other.spec").write_text("")
    with pytest.raises(RuntimeError, match=re.escape("grub has no grub.spec, and several other specs")):
        main_spec(tmp_path / "grub")


def removed(project: Project, removal: dict[str, list[Path]]) -> int:
    """Remove what `removal` has, as xcraft clean does, and return how many bytes that freed."""
    size = freed([p for paths in removal.values() for p in paths])
    remove(project, removal)
    return size


def should_keep_the_isos_of_the_checkout_when_cleaning(project: Project) -> None:
    isos, cache = project.state / "iso", project.state / "cache/isos" / project.arch
    isos.mkdir(parents=True)
    for key in ("current", "old"):
        (cache / key).mkdir(parents=True)
        (cache / key / "image.iso").write_bytes(key.encode() * 4096)
        (isos / f"{key}.iso").hardlink_to(cache / key / "image.iso")
    (isos / "copied.iso").write_bytes(b"current" * 4096)  # copied, by an xcraft of before, or elsewhere
    removal = routine_removal(project, [{}], isos={"current"})
    assert set(removal) == {"outdated ISOs", "ISOs in .xcraft/iso"}
    assert removed(project, removal) > 0
    assert sorted(p.name for p in cache.iterdir()) == ["current"]
    assert sorted(p.name for p in isos.iterdir()) == ["current.iso"]


def should_keep_sources_and_current_builds_when_cleaning(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    state = project.state
    for path in ("cache/sources/abc/foo.tar.gz", "cache/builds/one/x86_64_v2/foo/current/RPMS/foo.rpm",
                 "cache/builds/one/x86_64_v2/foo/old/RPMS/foo.rpm", "cache/builds/one/x86_64_v2/gone/old/RPMS/gone.rpm",
                 "build/foo/SPECS/foo.spec", "repo/foo.rpm", "ccache/foo/data",
                 # the caches of what this process read, and of the rest
                 "cache/specs/x86_64_v2/current.json", "cache/specs/x86_64_v2/old.json",
                 "cache/resolved/x86_64_v2/current.json", "cache/resolved/x86_64_v2/old.json",
                 # of another architecture: see xcraft clean --arch
                 "cache/specs/aarch64/old.json", "cache/resolved/aarch64/old.json",
                 "cache/checks/installable/old", "cache/upstream/old.json", "cache/rpms/old.rpm",
                 "cache/srpms/old.src.rpm", "cache/builds/key/x86_64_v2/foo/old/RPMS/foo.rpm",
                 "cache/builds/key/x86_64_v2/foo/current/RPMS/foo.rpm",
                 "cache/builds/key/aarch64/foo/old/RPMS/foo.rpm",
                 "logs/foo.log", "logs/gone.log", "logs/_resolve.log"):  # fmt: skip
        (state / path).parent.mkdir(parents=True, exist_ok=True)
        (state / path).write_bytes(b"x" * 8192)
    (project.root / "base/foo").mkdir(parents=True)  # but not gone, removed from the project
    monkeypatch.setattr(specs, "_read", {state / "cache/specs/x86_64_v2/current.json"})
    monkeypatch.setattr(resolution, "_resolved", {state / "cache/resolved/x86_64_v2/current.json": Resolved()})

    keys = {"foo": "current", "bar": "current"}
    # only what is of the given packages
    for path in ("cache/builds/one/x86_64_v2/bar/current/RPMS/bar.rpm",
                 "cache/builds/one/x86_64_v2/bar/old/RPMS/bar.rpm", "build/bar/SPECS/bar.spec",
                 "ccache/bar/data"):  # fmt: skip
        (state / path).parent.mkdir(parents=True, exist_ok=True)
        (state / path).write_bytes(b"x" * 8192)
    assert (
        removed(
            project, routine_removal(project, [keys], ["bar"], isos=set()) | caches_removal(project, "ccache", ["bar"])
        )
        > 0
    )
    assert "cache/builds/one/x86_64_v2/bar/current/RPMS/bar.rpm" in files(state)
    assert not [
        f for f in files(state) if f.startswith(("cache/builds/one/x86_64_v2/bar/old", "build/bar", "ccache/bar"))
    ]
    assert {"build/foo/SPECS/foo.spec", "cache/builds/one/x86_64_v2/foo/old/RPMS/foo.rpm"} <= set(files(state))
    # what the local repository, the snapshot and the repositories of xcraft repo still link
    rpm = state / "cache/builds/one/x86_64_v2/bar/current/RPMS/bar.rpm"
    for linked in (state / "repo/bar.rpm", state / "publish/base/x86_64_v2/bar.rpm", project.root / "repo/bar.rpm"):
        linked.parent.mkdir(parents=True, exist_ok=True)
        linked.hardlink_to(rpm)
    assert repos_disk_usage(project) == 0
    # still in repo/, of xcraft repo
    assert removed(project, routine_removal(project, [keys], ["bar"], isos=set(), current=True)) == 0
    assert not [f for f in files(state) if "bar" in f]
    assert repos_disk_usage(project) == 0  # of a directory xcraft repo did not make
    (project.root / "repo" / OUTPUT_MARKER).touch()
    assert repos_disk_usage(project) == 8192  # only repo/ has it now

    assert removed(project, routine_removal(project, [keys], isos=set())) > 0
    kept = [
        "cache/builds/key/aarch64/foo/old/RPMS/foo.rpm",
        "cache/resolved/aarch64/old.json",
        "cache/resolved/x86_64_v2/current.json",
        "cache/sources/abc/foo.tar.gz",
        "cache/specs/aarch64/old.json",
        "cache/specs/x86_64_v2/current.json",
        "logs/_resolve.log",
        "logs/foo.log",
    ]
    current = [
        "cache/builds/key/x86_64_v2/foo/current/RPMS/foo.rpm",
        "cache/builds/one/x86_64_v2/foo/current/RPMS/foo.rpm",
    ]
    assert files(state) == sorted([*kept, *current, "ccache/foo/data"])
    removed(project, routine_removal(project, [keys], isos=set(), current=True) | caches_removal(project, "ccache"))
    assert files(state) == kept
    # but those of the packages of the checkout with sources
    write_manifest(project.root / "base/foo/sources", {"foo-2.tar.gz": "0" * 64})
    (state / "cache/sources" / ("0" * 64)).mkdir()
    (state / "cache/sources" / ("0" * 64) / "foo-2.tar.gz").write_text("foo")
    removed(project, routine_removal(project, [keys], isos=set()) | sources_removal(project))
    kept.remove("cache/sources/abc/foo.tar.gz")
    assert files(state) == sorted([*kept, f"cache/sources/{'0' * 64}/foo-2.tar.gz"])


def should_keep_the_outdated_builds_still_used_when_cleaning(project: Project) -> None:
    state, now = project.state, time.time()
    for path in ("cache/builds/one/x86_64_v2/foo/old", "cache/builds/one/x86_64_v2/foo/used",
                 "cache/builds/one/x86_64_v2/foo/current",
                 "cache/builds/key/x86_64_v2/foo/old", "cache/builds/key/x86_64_v2/foo/used",
                 "cache/isos/x86_64_v2/old", "cache/isos/x86_64_v2/used",
                 "cache/sources/old", "cache/sources/used"):  # fmt: skip
        (state / path).mkdir(parents=True)
        (state / path / "file").write_bytes(b"x" * 8192)
    for path in ("cache/specs/x86_64_v2/old.json", "cache/resolved/x86_64_v2/used.json"):
        (state / path).parent.mkdir(parents=True)
        (state / path).write_text("{}")
    for path in state.rglob("old*"):
        os.utime(path, (now - 86400, now - 86400))
    # the current builds too, but the outdated ones used
    removal = routine_removal(project, [{"foo": "current"}], isos=set(), current=True, used_since=now - 3600)
    removal |= sources_removal(project, now - 3600)
    assert removal["current builds"] == [state / "cache/builds/one/x86_64_v2/foo/current"]
    assert removed(project, removal) > 0
    assert sorted(p.relative_to(state).as_posix() for p in state.rglob("*") if p.parent.name != "used") == [
        "cache", "cache/builds", "cache/builds/key", "cache/builds/key/x86_64_v2", "cache/builds/key/x86_64_v2/foo",
        "cache/builds/key/x86_64_v2/foo/used", "cache/builds/one", "cache/builds/one/x86_64_v2",
        "cache/builds/one/x86_64_v2/foo", "cache/builds/one/x86_64_v2/foo/used", "cache/isos", "cache/isos/x86_64_v2",
        "cache/isos/x86_64_v2/used", "cache/resolved", "cache/resolved/x86_64_v2", "cache/resolved/x86_64_v2/used.json",
        "cache/sources", "cache/sources/used", "cache/specs", "cache/specs/x86_64_v2",
    ]  # fmt: skip


def should_estimate_a_build_from_its_previous_duration(project: Project) -> None:
    assert previous_duration(project, "foo") is None
    log = project.state / "cache/builds/one/x86_64_v2/foo/key/build.log"
    log.parent.mkdir(parents=True)
    log.write_text("Wrote: foo.rpm\n\nreal\t4m13.250s\nuser\t9m1.000s\n")
    assert previous_duration(project, "foo") == 253.25


def should_list_the_packages_queued_to_build(project: Project) -> None:
    assert queued(project) == []
    queue = project.state / "build/.queue"
    queue.parent.mkdir(parents=True)
    queue.write_text(f"{os.getpid()}\nfoo\nbar\n")
    assert queued(project) == ["foo", "bar"]
    queue.write_text("999999999\nfoo\n")  # its build is gone
    assert queued(project) == []


def should_only_empty_directories_xcraft_made(project: Project, tmp_path: Path) -> None:
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
    other = tmp_path / "mirror"  # a repository, like a mirror: not xcraft's without its marker
    (other / "repodata").mkdir(parents=True)
    (other / "foo.rpm").write_text("")
    with pytest.raises(RepoError, match="has content xcraft did not make"):
        fresh_dir(project, other)


def should_list_the_upstream_build_dependencies_to_publish(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = Binary(name="kernel-headers", evr="6.12-1", arch="x86_64_v2", provides=[], requires=[])
    project.packages = {"kernel": Package(name="kernel", binaries=[binary])}
    resolved = Resolved(image=["bash", "gcc", "kernel-headers"], buildroots={"xen": ["zlib-devel", "gcc"]})
    monkeypatch.setattr("xcraft.resolution.resolve", lambda _: resolved)
    pin = Pin(evr="1-1", arch="x86_64_v2", sha256="ab" * 32, source="bash")
    lock = Lock(sources={"bash": "1-1"}, binaries={project.arch: {"bash": pin}}, runtime={project.arch: ["bash"]})
    save_lock(project, lock)
    assert build_dependencies(project) == ["gcc", "zlib-devel"]  # but ours, and those of the base repository


def should_reject_builds_unlike_what_the_spec_declares(project: Project, tmp_path: Path) -> None:
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
    # in the names including the release too
    for release, built in ((".xcpng", "+b2.xcpng"), ("", "+b2")):
        named = numbered.model_copy(
            update={
                "name": f"foo-rel_1.0_2.abc{release}",
                "autorev_name": f"foo-rel_1.0_2.abc%{{autorev}}{release}",  # as parsed
                "evr": f"1.0-2.abc{release}",
                "provides": [],
            }
        )
        project.parsed = {"foo": Package(name="foo", dist=".xcpng", binaries=[named])}
        named_made = {f"foo-rel_1.0_2.abc{built}": made["foo"] | {"evr": f"1.0-2.abc{built}", "provides": []}}
        (tmp_path / "made.json").write_text(json.dumps(named_made))
        _check_made(project, "foo", tmp_path, suffix="+b2")


def should_record_what_rpm_generated_once_resolvable(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    build = project.state / "cache/builds/one/x86_64_v2/foo/key"
    (build / "RPMS/x86_64").mkdir(parents=True)
    (build / "RPMS/x86_64/foo-1.0-1.x86_64.rpm").touch()
    binary = Binary(name="foo", evr="1.0-1", arch="x86_64", provides=["foo = 1.0-1"], requires=[])
    project.config.platforms = [project.platform]  # the one of the specs given
    project.parsed = {"foo": Package(name="foo", binaries=[binary])}
    # what cannot be recorded yet is kept as unresolved, to record it again next time
    monkeypatch.setattr(
        "xcraft.checks.resolve_rpm_deps", lambda *_: Resolved(errors={"foo": "nothing provides libbar.so.1"})
    )
    assert not record_rpm_deps(project, ["foo"], {"foo": "key"})
    kept = json.loads((build / "rpm-deps.json").read_text())
    assert (kept["requires"], kept["provides"], kept["unresolved"]) == ({}, {}, ["foo"])
    generated = Resolved(requires={"foo": ["bar"]}, provides={"foo": ["foo = 1.0-1", "pkgconfig(foo) = 1.0"]})
    monkeypatch.setattr("xcraft.checks.resolve_rpm_deps", lambda *_: generated)
    assert record_rpm_deps(project, ["foo"], {"foo": "key"})
    builds = load_builds(project)
    # not what the spec declares, and the versions as placeholders
    assert (builds.requires.shared, builds.provides.shared) == (
        {"foo": ["bar"]},
        {"foo": ["pkgconfig(foo) = %{version}"]},
    )
    monkeypatch.setattr("xcraft.checks.resolve_rpm_deps", lambda *_: pytest.fail("recorded again"))
    assert not record_rpm_deps(project, ["foo"], {"foo": "key"})
    # but with a build of ours, which it may resolve to instead: a new one, or another of the same package
    baz = Binary(name="baz", evr="1.0-1", arch="x86_64", provides=[], requires=[])
    project.parsed = {"foo": Package(name="foo", binaries=[binary]), "baz": Package(name="baz", binaries=[baz])}
    for key, provider in (("first", "baz"), ("second", "bar")):
        (rpms := project.state / f"cache/builds/one/x86_64_v2/baz/{key}/RPMS/x86_64").mkdir(parents=True)
        (rpms / "baz-1.0-1.x86_64.rpm").touch()
        monkeypatch.setattr(
            "xcraft.checks.resolve_rpm_deps", lambda *_, provider=provider: Resolved(requires={"foo": [provider]})
        )
        assert record_rpm_deps(project, ["foo"], {"foo": "key", "baz": key})
        assert load_builds(project).requires.shared == {"foo": [provider]}


def should_record_providers_by_their_names_in_the_spec(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    # foo needs baz-rel, a package of ours named with its release
    keys = {"foo": "1" * 64 + "+b1", "baz": "2" * 64 + "+b3"}
    for name, rpm in (("foo", "foo-1.0-1+b1.x86_64.rpm"), ("baz", "baz-rel_1_1+b3-1.0-1+b3.x86_64.rpm")):
        (build := project.state / f"cache/builds/one/x86_64_v2/{name}/{keys[name]}/RPMS/x86_64").mkdir(parents=True)
        (build / rpm).touch()
    foo = Binary(name="foo", evr="1.0-1", arch="x86_64", provides=[], requires=[])
    named = Binary(name="baz-rel_1_1", autorev_name="baz-rel_1_1%{autorev}", evr="1.0-1", arch="x86_64", provides=[],
                requires=[])  # fmt: skip
    project.config.platforms = [project.platform]  # the one of the specs given
    project.parsed = {"foo": Package(name="foo", binaries=[foo]), "baz": Package(name="baz", binaries=[named])}
    monkeypatch.setattr("xcraft.checks.resolve_rpm_deps", lambda *_: Resolved(requires={"foo": ["baz-rel_1_1+b3"]}))
    assert record_rpm_deps(project, ["foo"], keys)
    # by its name in the spec, whatever its build
    assert load_builds(project).requires.shared == {"foo": ["baz-rel_1_1"]}


def should_fail_builds_still_to_build_again_after_retries(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    project.deps = {"a": set()}
    monkeypatch.setattr(resolution, "prepare", lambda _: None)
    monkeypatch.setattr(build, "built_image", lambda _: "image")
    monkeypatch.setattr(build, "_record_cached", lambda *_: ({"a": "key"}, {}, {}))
    monkeypatch.setattr(build, "update_build_ids", lambda _: None)
    attempts = []
    monkeypatch.setattr(build._Run, "build", lambda *_: attempts.append(1) or [build.Result("a", "again")])
    assert build.build(project, ["a"]) == ([build.Result("a", "failed")], {"a": "key"}, {})
    assert len(attempts) == 2


def should_build_packages_as_they_were_when_the_builds_started(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build, "record_rpm_deps", lambda *_: False)
    for name in ("a", "b", "c"):
        (project.root / "base" / name).mkdir(parents=True)
        (project.root / "base" / name / f"{name}.spec").write_text(name)
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ours(project, monkeypatch, {"a": set(), "b": {"a"}, "c": set()})
    hashes = buildkey.directories(project)
    keys = build_keys(project, hashes=hashes)
    (cache.cached_build(project, "b", keys["b"])).mkdir(parents=True)

    def building(_run: build._Run, name: str, _status: str | None) -> build.Result:
        if name == "c":  # a, which b depends on, edited meanwhile
            (project.root / "base/a/a.spec").write_text("a edited")
        cache.cached_build(project, name, keys[name]).mkdir(parents=True)
        return build.Result(name, "built")

    monkeypatch.setattr(build._Run, "_build_shown", building)
    monkeypatch.setattr(build._Run, "_lint_specs", lambda *_: None)  # of specs that are not
    (project.state / "build").mkdir(parents=True)  # for the queue, which build() makes
    results = build._Run(project, hashes, keys, {}, fetch="none").build(["a", "c", "b"])
    # the builds work on the packages as they were at the start: b fails, rather than b cached with the old a
    assert results == [build.Result("a", "built"), build.Result("c", "built"), build.Result("b", "failed")]


def should_build_again_when_the_last_recording_changes_a_key(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a", "b"):
        (project.root / "base" / name).mkdir(parents=True)
        (project.root / "base" / name / f"{name}.spec").write_text(name)
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ours(project, monkeypatch, {"a": set(), "b": {"a"}})
    monkeypatch.setattr(resolution, "prepare", lambda _: None)
    hashes = buildkey.directories(project)
    keys = build_keys(project, hashes=hashes)
    for name in ("a", "b"):
        cache.cached_build(project, name, keys[name]).mkdir(parents=True)

    def record(*_: object, quiet: bool = False) -> bool:
        # the last one, of the cached builds, finds what changes the build root of b
        if quiet:
            return False
        builds = load_builds(project)
        builds.buildrequires.shared["b"] = ["found"]
        save_builds(project, builds)
        return True

    monkeypatch.setattr(build, "record_rpm_deps", record)
    (project.state / "build").mkdir(parents=True)  # for the queue, which build() makes
    run = build._Run(project, hashes, keys, {}, fetch="none")
    results, after = run.build(["a", "b"]), run.keys
    # b to build again, with its new key, not cached
    assert results == [build.Result("a", "cached"), build.Result("b", "again")]
    assert (after["a"], after["b"]) == (keys["a"], after["b"])
    assert after["b"] != keys["b"]


def should_build_again_when_a_recording_changes_a_key(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a", "b", "c"):
        (project.root / "base" / name).mkdir(parents=True)
        (project.root / "base" / name / f"{name}.spec").write_text(name)
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ours(project, monkeypatch, {"a": set(), "b": {"a"}, "c": set()})
    monkeypatch.setattr(resolution, "prepare", lambda _: None)
    hashes = buildkey.directories(project)
    keys = build_keys(project, hashes=hashes)
    for name in ("a", "b", "c"):
        cache.cached_build(project, name, keys[name]).mkdir(parents=True)
    monkeypatch.setattr(build, "record_rpm_deps", lambda *_, **__: False)  # the last recording changes nothing
    done = build._Run._done

    def changing(run: build._Run, name: str) -> build.Result | None:
        if name == "c":  # what changes the build root of b, found after it, like by another command
            builds = load_builds(project)
            builds.buildrequires.shared["b"] = ["found"]
            save_builds(project, builds)
        return done(run, name)

    monkeypatch.setattr(build._Run, "_done", changing)
    (project.state / "build").mkdir(parents=True)  # for the queue, which build() makes
    run = build._Run(project, hashes, keys, {}, fetch="none")
    results, after = run.build(["a", "b", "c"]), run.keys
    # b to build again, with its new key, not cached
    assert results == [build.Result("a", "cached"), build.Result("b", "again"), build.Result("c", "cached")]
    assert after["b"] != keys["b"]


def should_build_again_when_stored_builds_have_former_keys(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "record_rpm_deps", lambda *_, **__: False)
    keys = {"a": "1" * 64, "b": "2" * 64}
    stored = {f"a/{keys['a']}": True, f"b/{'3' * 64}": True}
    results = {name: build.Result(name, "stored") for name in keys}
    # b in the storage with the key it had then, not with the final one: fetched or built again
    after = build._Run(project, {}, keys, {}, results=results, stored=stored)._recorded_again()
    assert after == [build.Result("a", "stored"), build.Result("b", "again")]


def should_fail_when_the_recordings_keep_changing_the_keys(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(build, "dev_keys", lambda *_: ({}, {}))
    monkeypatch.setattr(build, "record_rpm_deps", lambda *_, **__: True)
    monkeypatch.setattr(resolution, "prepare", lambda _: None)
    with pytest.raises(BuildError, match="keeps changing the keys"):
        build._record_cached(project, None)
