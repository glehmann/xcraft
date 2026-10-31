"""The build keys and the cache keys: their formulas, and what changes them, as docs/build-keys.md specifies."""

from __future__ import annotations

import hashlib
import json
import re
from typing import TYPE_CHECKING

import pytest

from tests import TOML, config, ours
from xcraft import buildid, buildkey
from xcraft.buildid import BuildId
from xcraft.buildkey import recipe_key
from xcraft.cache import build_keys
from xcraft.lock import Lock, Pin, Recorded, save_lock
from xcraft.project import Project
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "xcraft.toml").write_text(TOML)
    project = Project(tmp_path, config())
    project.config.platforms = [project.platform]  # the one of the dependencies given
    return project


def test_formulas() -> None:
    # fixed values: a change of the formulas, or of how their inputs are serialized, changes all the keys
    inputs = {"a": ["1" * 64, "2" * 64, {"x86_64_v2": [{"glibc": "2.39-1"}, {}, [], {}]}, [["b", 2, "3" * 64]]]}
    assert buildkey.key(inputs, "a") == "d253f2c4df8681e755fec12f9fd0c205524908f739f3bdfd7ba0b4b5fbd33401"
    assert (
        buildkey.development_key({"a": ["1" * 64, "~1.dev"]}, {"b": "3" * 64 + "+b2"}, "a")
        == "d21c169c89066b40b20382eee1576e8c1cdd7cd8e58878e5bf18cf7458f7b5f9"
    )
    # as the specification has them
    assert buildkey.key(inputs, "a") == hashlib.sha256(f"{json.dumps(inputs, sort_keys=True)}\0a".encode()).hexdigest()


def test_build_key_inputs(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    arch = project.arch
    # b built against a: the binary packages of a, what they need, and what it needs to be built
    ours(project, monkeypatch, {"a": set(), "b": {"a"}})
    hashes = {"a": "1" * 64, "b": "2" * 64}
    pin = Pin(evr="1-1", arch="noarch", sha256="1" * 64, source="glibc")
    pins = {
        "glibc": pin,
        "zlib": pin.model_copy(update={"source": "zlib"}),
        "bash": pin.model_copy(update={"source": "bash"}),
    }
    lock = Lock(
        sources={"glibc": "1-1", "zlib": "1-1", "bash": "1-1"},
        binaries={arch: pins},
        image={arch: ["glibc"]},
        roots=Recorded(shared={"b": ["zlib"]}),
    )

    def repin(name: str, **update: str) -> None:
        lock.binaries[arch][name] = lock.binaries[arch][name].model_copy(update=update)

    def keys() -> dict[str, str]:
        save_lock(project, lock)
        return {name: i.key for name, i in buildid.compute(project, None, hashes).items()}

    before = keys()
    for change, changed in (
        (lambda: repin("glibc", sha256="2" * 64), {"a", "b"}),  # of the image: other contents, of the same version
        (lambda: repin("glibc", evr="1-2"), {"a", "b"}),
        (lambda: repin("zlib", sha256="2" * 64), {"b"}),  # of the build root of b
        (lambda: repin("bash", sha256="2" * 64), set()),  # of no image nor build root of the project
        (lambda: lock.image[arch].append("bash"), {"a", "b"}),
        (lambda: lock.sources.update(zlib="1-2"), set()),  # what its binary packages have
        (lambda: lock.buildrequires.shared.update(b=["foo"]), {"b"}),
        (lambda: lock.requires.shared.update(a=["bar"]), {"b"}),  # of the binary package a, which b gets
        (lambda: lock.provides.shared.update(a=["libbar.so.1"]), {"b"}),
        # not an architecture of the project
        (
            lambda: lock.binaries.update(aarch64=lock.binaries[arch]) or lock.roots.arches.update(aarch64={"b": []}),
            set(),
        ),
        (lambda: lock.runtime.update({arch: ["bash"]}), set()),  # what publishing gets
        (lambda: lock.iso.update({arch: ["xorriso"]}), set()),
        (lambda: hashes.update(a="3" * 64), {"a", "b"}),  # the files of a, which change its build ID
        (lambda: hashes.update(b="4" * 64), {"b"}),
    ):
        change()
        after = keys()
        assert {name for name in after if after[name] != before[name]} == changed, change
        before = after


def test_inputs_read_once(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    ours(project, monkeypatch, {"a": set(), "b": {"a"}, "c": set()})
    save_lock(project, Lock())
    hashes = {name: digit * 64 for name, digit in zip("abc", "123", strict=True)}
    expected = buildid.compute(project, None, hashes)
    # not the dependencies the project has from another lock, read before
    project.deps = {"a": set(), "b": set(), "c": {"b"}}
    assert buildid.compute(project, None, hashes) == expected
    # but those of the lock given, like the one of another commit, with its dynamic BuildRequires
    other = buildkey.Inputs(project, hashes, Lock(buildrequires=Recorded(shared={"c": ["a"]})))
    assert (other.deps["c"], buildkey.Inputs(project, hashes).deps["c"]) == ({"a"}, set())
    # and nothing more after
    monkeypatch.setattr(buildkey, "load_lock", lambda _: pytest.fail("read again"))
    monkeypatch.setattr("xcraft.resolution.resolve_image", lambda _: pytest.fail("resolved again"))
    project.parsed = {}
    ids = {name: BuildId(number=1, key=hashes[name]) for name in hashes}
    assert buildkey.key(other.of({"c"}, ids), "c") != buildkey.key(other.of({"a"}, ids), "a")


def test_image_of_the_lock(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    ours(project, monkeypatch, {"a": set(), "b": set()})
    save_lock(project, Lock())
    # a lock of another commit, whose image has a binary package that a of ours replaces, like kernel-headers: b is
    # built against a there, not with the current lock
    pin = Pin(evr="1-1", arch="noarch", sha256="1" * 64, source="a-upstream")
    other = Lock(sources={"a-upstream": "1-1"}, binaries={project.arch: {"a": pin}}, image={project.arch: ["a"]})
    hashes = {"a": "1" * 64, "b": "2" * 64}
    assert (buildkey.Inputs(project, hashes, other).deps["b"], buildkey.Inputs(project, hashes).deps["b"]) == (
        {"a"},
        set(),
    )


def test_lock_inputs_of_reused_builds() -> None:
    pin = Pin(evr="1-1", arch="noarch", sha256="1" * 64, source="zlib")
    platforms = [buildkey.Platform("x86_64_v2", {"b": frozenset()}, {"b": ()})]

    def inputs(*binaries: tuple[str, Pin]) -> dict[str, list]:
        lock = Lock(
            sources={"zlib": "1-1", "zlib-ng": "1-1"},
            binaries={"x86_64_v2": dict(binaries)},
            image={"x86_64_v2": []},
            roots=Recorded(shared={"b": [name for name, _ in binaries]}),
        )
        return buildkey.lock_inputs(platforms, lock, "b", {"zlib"})

    before = inputs(("zlib", pin))
    # an update of zlib, which xcraft reuse --dep ignores: other versions and contents of the same packages
    assert inputs(("zlib", pin.model_copy(update={"evr": "1-2", "sha256": "2" * 64}))) == before
    # but not other packages, nor packages of another source
    assert inputs(("zlib", pin), ("zlib-ng", pin)) != before
    assert inputs(("zlib", pin.model_copy(update={"source": "zlib-ng"}))) != before


def test_keys_of_cycles(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a", "b", "0foo"):
        (project.root / "base" / name).mkdir(parents=True)
        (project.root / "base" / name / f"{name}.spec").write_text(name)
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ours(project, monkeypatch, {"a": {"b"}, "b": {"a"}, "0foo": set()})
    keys = build_keys(project)
    # not the order the cycle is reached in: from a package depending on b, first in name order
    ours(project, monkeypatch, {"a": {"b"}, "b": {"a"}, "0foo": {"b"}})
    more = build_keys(project)
    assert (more["a"], more["b"]) == (keys["a"], keys["b"])
    assert keys["a"] != keys["b"]
    (project.root / "base/b/b.spec").write_text("b changed")
    ours(project, monkeypatch, {"a": {"b"}, "b": {"a"}, "0foo": set()})
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
    script.chmod(0o700)  # the bit of its owner, as git keeps it, whoever reads it
    assert hash_dir(pkg) == executable
    (pkg / "sub/origin").write_text("a source")
    assert hash_dir(pkg) != executable
    # a link is the file of the project it points to, like the sources of another package
    shared = tmp_path / "shared"
    shared.write_text("shared")
    (pkg / "linked").symlink_to(shared)
    linked = hash_dir(pkg, tmp_path)
    shared.write_text("changed")
    assert hash_dir(pkg, tmp_path) != linked
    # not one out of it, which depends on the machine, nor a directory, nor nothing
    for target in (shared, tmp_path, tmp_path / "missing"):
        (pkg / "linked").unlink()
        (pkg / "linked").symlink_to(target)
        with pytest.raises(ValueError, match="is a link to"):
            hash_dir(pkg, pkg if target == shared else tmp_path)


def test_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    current = "0cbdc798545bf78d6f525d232ef6af43bae61cab8de1b5f5ac7bb2b39709a6bf"
    assert recipe_key() == current, (
        f"the recipe changed, to {buildkey.RECIPE}: map it to {current} in _SAME_BUILDS if it makes the same packages, "
        "or else set it here, which rebuilds everything"
    )
    # hashes of earlier recipes, each mapped to the earliest one, not to another mapped one
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for item in buildkey._SAME_BUILDS.items() for h in item)
    assert not buildkey._SAME_BUILDS.keys() & set(buildkey._SAME_BUILDS.values())
    monkeypatch.setattr(buildkey, "RECIPE", "c")
    monkeypatch.setattr(buildkey, "_SAME_BUILDS", {"c": "a", "b": "a"})
    assert recipe_key() == "a"


def test_recipe_inputs(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    config, before = project.config, buildkey.recipe(project)
    # not what the builds do not get: where the source packages are published from, the order of the configuration
    config.repos["baseos"].source_baseurl = "https://example.com/elsewhere/"
    assert buildkey.recipe(project) == before
    config.macros = {**config.macros, "a": "1", "b": "2"}
    after = buildkey.recipe(project)
    config.macros, config.repos = dict(reversed(config.macros.items())), dict(reversed(config.repos.items()))
    assert buildkey.recipe(project) == after
    # but all the rest
    for change in (
        lambda: setattr(config, "macros", {**config.macros, "a": "changed"}),
        lambda: setattr(config.repos["baseos"], "baseurl", "https://example.com/other/"),
        lambda: setattr(config.repos["baseos"], "includepkgs", ["bash"]),
        lambda: setattr(config, "packages", [*config.packages, "vim"]),
        lambda: setattr(config, "release_suffixes", [".alt"]),
        lambda: setattr(config, "extra_upstream", {"foo": "https://example.com/foo.rpm"}),
        lambda: setattr(config, "base_image", config.base_image.replace("@sha256:", "@sha256:0")[:-1]),
        lambda: monkeypatch.setattr(buildkey, "FORMAT", buildkey.FORMAT + 1),
        lambda: monkeypatch.setattr(buildkey, "RECIPE", "0" * 64),
    ):
        change()
        assert buildkey.recipe(project) != after
        after = buildkey.recipe(project)
    assert after != before


def test_keys_of_upstream_packages_without_versions(project: Project) -> None:
    with pytest.raises(RuntimeError, match="the lock has no pinned glibc"):
        buildkey.lock_inputs(
            [buildkey.Platform(project.arch, {}, {})], Lock.model_construct(image={project.arch: ["glibc"]}), "foo"
        )


def test_cache_keys_of_development_builds(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ids = {name: BuildId(number=1, key=digit * 64) for name, digit in zip("abcde", "12345", strict=True)}

    def keys(deps: dict[str, set[str]], autorev: dict[str, str]) -> dict[str, str]:
        ours(project, monkeypatch, deps)
        return build_keys(project, autorev, ids=ids)

    chain = {"a": set(), "b": {"a"}, "c": {"b"}}
    # final builds, against final builds: their build keys
    assert keys(chain, {}) == {name: f"{ids[name].key}+b1" for name in chain}
    # a development build of a: those built against it, even through a build of a final suffix, have other keys
    first, second = keys(chain, {"a": "~1.dev"}), keys(chain, {"a": "~2.dev"})
    assert first["a"].endswith("+b1~1.dev")
    assert first["b"].endswith("+b1")
    assert all(first[name] != f"{ids[name].key}+b1" for name in chain)
    assert all(first[name] != second[name] for name in chain)
    # in a cycle of the dependencies of all the architectures: those of each development build of the others
    cycle = {"d": {"e"}, "e": {"d"}}
    assert keys(cycle, {"d": "~1.dev", "e": "~1.dev"})["d"] != keys(cycle, {"d": "~1.dev", "e": "~2.dev"})["d"]
