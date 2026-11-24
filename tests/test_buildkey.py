"""The build keys and the cache keys: their formulas, and what changes them, as docs/build-keys.md specifies."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import pytest

from tests import TOML, config, ours
from xcraft import buildid, buildkey
from xcraft.buildkey import recipe_key
from xcraft.builds import BuildId, Builds, save_builds
from xcraft.cache import build_keys
from xcraft.lock import Lock, Pin, Recorded, save_lock
from xcraft.project import Extra, Project
from xcraft.specs import hash_dir

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "xcraft.toml").write_text(TOML)
    project = Project(tmp_path, config())
    project.config.platforms = [project.platform]  # the one of the dependencies given
    return project


def should_keep_the_keys_of_the_same_inputs() -> None:
    # fixed values: a change of the formulas, or of how their inputs are serialized, changes all the keys
    inputs = {"a": ["1" * 64, "2" * 64, {"x86_64_v2": [{"glibc": "2.39-1"}, {}, [], {}]}, [["b", 2, "3" * 64]]]}
    assert buildkey.key(inputs, "a") == "d253f2c4df8681e755fec12f9fd0c205524908f739f3bdfd7ba0b4b5fbd33401"
    assert (
        buildkey.development_key({"a": ["1" * 64, "~1.dev"]}, {"b": "3" * 64 + "+b2"}, "a")
        == "d21c169c89066b40b20382eee1576e8c1cdd7cd8e58878e5bf18cf7458f7b5f9"
    )


def should_change_the_keys_of_the_packages_whose_inputs_changed(
    project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    arch = project.arch
    # b built against a: the binary packages of a, what they need, and what it needs to be built
    ours(project, monkeypatch, {"a": set(), "b": {"a"}})
    for name in ("a", "b"):
        (project.root / "base" / name).mkdir(parents=True)
    hashes = {"a": "1" * 64, "b": "2" * 64}
    builds = Builds()
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
        save_builds(project, builds)
        return {name: i.key for name, i in buildid.compute(project, None, hashes).items()}

    def lock_other_arch() -> None:
        lock.binaries["aarch64"] = lock.binaries[arch]
        lock.roots.arches["aarch64"] = {"b": []}

    before = keys()
    for description, change, changed in (
        ("other contents of a package of the image", lambda: repin("glibc", sha256="2" * 64), {"a", "b"}),
        ("another version of a package of the image", lambda: repin("glibc", evr="1-2"), {"a", "b"}),
        ("other contents of a package of the build root of b", lambda: repin("zlib", sha256="2" * 64), {"b"}),
        ("other contents of a package of no image nor build root", lambda: repin("bash", sha256="2" * 64), set()),
        ("another package in the image", lambda: lock.image[arch].append("bash"), {"a", "b"}),
        ("another version of a source, which its binaries have", lambda: lock.sources.update(zlib="1-2"), set()),
        ("a dynamic BuildRequires of b", lambda: builds.buildrequires.shared.update(b=["foo"]), {"b"}),
        ("a requirement generated for a, which b gets", lambda: builds.requires.shared.update(a=["bar"]), {"b"}),
        ("a capability generated for a", lambda: builds.provides.shared.update(a=["libbar.so.1"]), {"b"}),
        ("the lock of an architecture not of the project", lock_other_arch, set()),
        ("what publishing gets", lambda: lock.runtime.update({arch: ["bash"]}), set()),
        ("the tools making the ISO", lambda: lock.iso.update({arch: ["bash"]}), set()),
        ("the files of a, which change its build ID", lambda: hashes.update(a="3" * 64), {"a", "b"}),
        ("the files of b", lambda: hashes.update(b="4" * 64), {"b"}),
    ):
        change()
        after = keys()
        assert {name for name in after if after[name] != before[name]} == changed, description
        before = after


def should_compute_the_keys_from_the_inputs_read_once(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    ours(project, monkeypatch, {"a": set(), "b": {"a"}, "c": set()})
    save_lock(project, Lock())
    hashes = {name: digit * 64 for name, digit in zip("abc", "123", strict=True)}
    expected = buildid.compute(project, None, hashes)
    # not the dependencies the project has from another lock, read before
    project.deps = {"a": set(), "b": set(), "c": {"b"}}
    assert buildid.compute(project, None, hashes) == expected
    # but those of the locks given, like those of another commit, with its dynamic BuildRequires
    other = buildkey.Inputs(project, hashes, Lock(), Builds(buildrequires=Recorded(shared={"c": ["a"]})))
    assert other.deps["c"] == {"a"}
    ids = {name: BuildId(number=1, key=hashes[name]) for name in hashes}
    key = buildkey.key(other.of({"c"}, ids), "c")
    # and nothing more after: neither the locks, the files, nor the specs changed since
    save_lock(project, Lock(sources={"glibc": "1-1"}))
    save_builds(project, Builds(buildrequires=Recorded(shared={"c": ["b"]})))
    hashes["c"] = "4" * 64
    project.parsed = {}
    assert buildkey.key(other.of({"c"}, ids), "c") == key


def should_take_the_image_from_the_lock_given(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
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


def should_cover_the_architectures_a_package_is_built_for_only() -> None:
    pin = Pin(evr="1-1", arch="noarch", sha256="1" * 64, source="zlib")
    # its spec excludes aarch64
    platforms = [
        buildkey.Platform("x86_64_v2", {"b": frozenset()}, {"b": ()}),
        buildkey.Platform("aarch64", {}, {}),
    ]

    def inputs(arm: Pin) -> dict[str, list]:
        lock = Lock(sources={"zlib": "1-1"}, binaries={"x86_64_v2": {"zlib": pin}, "aarch64": {"zlib": arm}})
        lock.image = {"x86_64_v2": ["zlib"], "aarch64": ["zlib"]}
        return buildkey.lock_inputs(platforms, lock, Builds(), "b")

    assert list(inputs(pin)) == ["x86_64_v2"]
    assert inputs(pin.model_copy(update={"evr": "1-2", "sha256": "2" * 64})) == inputs(pin)


def should_ignore_dependency_updates_for_reused_builds() -> None:
    pin = Pin(evr="1-1", arch="noarch", sha256="1" * 64, source="zlib")
    platforms = [buildkey.Platform("x86_64_v2", {"b": frozenset()}, {"b": ()})]

    def inputs(*binaries: tuple[str, Pin]) -> dict[str, list]:
        lock = Lock(
            sources={"zlib": "1-1", "zlib-ng": "1-1"},
            binaries={"x86_64_v2": dict(binaries)},
            image={"x86_64_v2": []},
            roots=Recorded(shared={"b": [name for name, _ in binaries]}),
        )
        return buildkey.lock_inputs(platforms, lock, Builds(), "b", {"zlib"})

    before = inputs(("zlib", pin))
    # an update of zlib, which xcraft reuse --built-against ignores: other versions and contents of the same packages
    assert inputs(("zlib", pin.model_copy(update={"evr": "1-2", "sha256": "2" * 64}))) == before
    # but not other packages, nor packages of another source
    assert inputs(("zlib", pin), ("zlib-ng", pin)) != before
    assert inputs(("zlib", pin.model_copy(update={"source": "zlib-ng"}))) != before


def should_give_cycles_the_same_keys_whatever_the_order(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
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


def should_hash_the_package_files_as_builds_get_them(tmp_path: Path) -> None:
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
    for target, root in ((shared, pkg), (tmp_path, tmp_path), (tmp_path / "missing", tmp_path)):
        (pkg / "linked").unlink()
        (pkg / "linked").symlink_to(target)
        with pytest.raises(ValueError, match="is a link to"):
            hash_dir(pkg, root)


def should_map_earlier_recipes_making_the_same_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    current = "6bec4158f5303f57a1b4996c6901e214bf126dff1755fa651a643659f4221144"
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


def should_change_the_recipe_only_for_what_builds_get(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    config, before = project.config, buildkey.recipe(project)
    # not what the builds do not get: where the source packages are published from, the order of the configuration
    config.repos["baseos"].source_baseurl = "https://example.com/elsewhere/"
    assert buildkey.recipe(project) == before
    config.macros = {**config.macros, "a": "1", "b": "2"}
    after = buildkey.recipe(project)
    config.macros, config.repos = dict(reversed(config.macros.items())), dict(reversed(config.repos.items()))
    assert buildkey.recipe(project) == after
    # but all the rest
    extra = {"foo": Extra(url="https://example.com/foo.rpm", sha256="1" * 64)}
    for description, change in (
        ("a macro", lambda: setattr(config, "macros", {**config.macros, "a": "changed"})),
        ("the URL of a repository", lambda: setattr(config.repos["baseos"], "baseurl", "https://example.com/other/")),
        ("the packages of a repository", lambda: setattr(config.repos["baseos"], "includepkgs", ["bash"])),
        ("the packages of the image", lambda: setattr(config, "packages", [*config.packages, "vim"])),
        ("the release suffixes", lambda: setattr(config, "release_suffixes", [".alt"])),
        ("the extra upstream RPMs", lambda: setattr(config, "extra_upstream", extra)),
        (
            "the base image",
            lambda: setattr(config, "base_image", config.base_image.replace("@sha256:", "@sha256:0")[:-1]),
        ),
        (
            "how xcraft gives the builds their inputs",
            lambda: monkeypatch.setattr(buildkey, "FORMAT", buildkey.FORMAT + 1),
        ),
        ("the scripts of the builds", lambda: monkeypatch.setattr(buildkey, "RECIPE", "0" * 64)),
    ):
        change()
        assert buildkey.recipe(project) != after, description
        after = buildkey.recipe(project)
    assert after != before


def should_fail_when_the_lock_pins_nothing_for_the_shared_build_roots_of_an_architecture(project: Project) -> None:
    # shared by the architectures, valid without one pinning their packages
    lock = Lock(sources={"zlib": "1-1"}, roots=Recorded(shared={"foo": ["zlib"]}))
    with pytest.raises(RuntimeError, match=f"the lock has no pinned zlib for {project.arch}"):
        buildkey.lock_inputs([buildkey.Platform(project.arch, {}, {"foo": ()})], lock, Builds(), "foo")


def should_give_new_keys_to_builds_against_development_ones(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
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


def should_change_the_keys_with_the_image_of_any_architecture(root: Path) -> None:
    # the repositories of the build environment differ by architecture, not the build keys
    projects = [Project.find(arch) for arch in ("x86_64_v2", "aarch64")]
    assert projects[0].repos.keys() != projects[1].repos.keys()
    assert buildkey.recipe(projects[0]) == buildkey.recipe(projects[1])
    # which the pinned binary packages of the image of each architecture change
    inputs = buildkey.Inputs(projects[0])
    before = buildkey.key(inputs.of({"trivial"}, {}), "trivial")
    assert before == buildid.compute(projects[0], None)["trivial"].key
    for arch in ("x86_64_v2", "aarch64"):
        name = inputs.lock.image[arch][0]
        pin = inputs.lock.binaries[arch][name]
        inputs.lock.binaries[arch][name] = pin.model_copy(update={"sha256": "0" * 64})
        assert buildkey.key(inputs.of({"trivial"}, {}), "trivial") != before
        inputs.lock.binaries[arch][name] = pin
