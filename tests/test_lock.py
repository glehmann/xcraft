from __future__ import annotations

import pytest

from tests import TOML, config
from xcraft.lock import Lock, Locked, Recorded, choose_versions, load_lock, lock_path, save_lock, versionlock
from xcraft.project import Project
from xcraft.resolution import lock_sources


def locked(evr: str, source: str = "foo", arch: str = "x86_64_v2") -> Locked:
    return Locked(evr=evr, arch=arch, source=source, repo="appstream", sha256="ab" * 32)


@pytest.fixture
def project(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Project:  # noqa: ANN001
    (tmp_path / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    monkeypatch.delenv("XCRAFT_ARCH", raising=False)
    return Project.find()


def test_save_and_load(tmp_path) -> None:  # noqa: ANN001
    project = Project(tmp_path, config())
    lock = Lock(
        sources={"glibc": "2.39-145.el10_2", "openssl": "1:3.5.8-1.el10_2"},
        requires=Recorded(shared={"qemu": ["libpng", "pixman"]}, arches={"aarch64": {"qemu": ["pixman"]}}),
        buildrequires=Recorded(shared={"xcp-python-libs": ["python3-wheel"]}),
        runtime={"x86_64_v2": ["bash"]},
        iso={"aarch64": ["xorriso"]},
        image={"aarch64": ["glibc"]},
        roots=Recorded(shared={"qemu": ["pixman"]}),
    )
    save_lock(project, lock)
    assert lock_path(project) == tmp_path / "xcraft.lock"
    assert load_lock(project) == lock
    text = lock_path(project).read_text()
    # one line per package, for readable diffs, and the differences of an architecture in its own table
    assert '[sources]\n"glibc" = "2.39-145.el10_2"\n' in text
    assert '[requires.aarch64]\n"qemu" = [\n    "pixman",\n]\n' in text
    assert '[roots]  # the upstream source packages of the build roots, but the image\'s\n"qemu" = [\n' in text


def test_record() -> None:
    recorded = Recorded()
    # what another architecture finds first is shared, until the first architecture finds it otherwise
    recorded.record("aarch64", "x86_64_v2", "qemu", ["pixman", "libpng"])
    assert (recorded.shared, recorded.arches) == ({"qemu": ["libpng", "pixman"]}, {})
    recorded.record("x86_64_v2", "x86_64_v2", "qemu", ["pixman", "libpng", "libaio"])
    assert recorded.of("aarch64") == {"qemu": ["libaio", "libpng", "pixman"]}
    # then the other architectures have their own entries, which replace the shared ones, with less too
    recorded.record("aarch64", "x86_64_v2", "qemu", ["pixman"])
    recorded.record("aarch64", "x86_64_v2", "xen", ["lzo"])
    recorded.record("aarch64", "x86_64_v2", "vmss", [])
    assert recorded.arches == {"aarch64": {"qemu": ["pixman"]}}
    assert recorded.of("aarch64") == {"qemu": ["pixman"], "xen": ["lzo"]}
    recorded.record("aarch64", "x86_64_v2", "xen", [])
    assert recorded.of("aarch64") == {"qemu": ["pixman"], "xen": []}
    # finding the shared entry again removes their own
    recorded.record("aarch64", "x86_64_v2", "qemu", ["libaio", "libpng", "pixman"])
    recorded.record("aarch64", "x86_64_v2", "xen", ["lzo"])
    assert recorded.arches == {}
    recorded.record("x86_64_v2", "x86_64_v2", "xen", [])
    assert recorded.shared == {"qemu": ["libaio", "libpng", "pixman"]}


def test_versionlock() -> None:
    packages = {"gcc": locked("1:14.2-1"), "bash": locked("5.2-3")}
    assert versionlock(packages) == "bash-0:5.2-3.x86_64_v2\ngcc-1:14.2-1.x86_64_v2\n"


def test_frozen(tmp_path) -> None:  # noqa: ANN001
    project = Project(tmp_path, config())
    lock = Lock(sources={"bash": "5.2-3"}, requires=Recorded(shared={"xen-dom0-libs": ["lzo"]}))
    save_lock(project, lock)
    project.frozen = True
    save_lock(project, lock)  # unchanged
    lock.sources["zlib-ng"] = "2.2-1"
    lock.requires.record("aarch64", "x86_64_v2", "xen-dom0-libs", [])
    with pytest.raises(
        RuntimeError,
        match=r"--locked forbids: \+requires\.aarch64\.xen-dom0-libs \(none\), \+sources zlib-ng-2\.2-1$",
    ):
        save_lock(project, lock)
    assert "zlib-ng" not in load_lock(project).sources


def test_choose_versions() -> None:
    x86 = {"glibc": ["2.39-142", "2.39-145", "2.39-146"], "macros": ["1-21.el10", "1-22.el10"], "lcov": ["2.0-5"]}
    arm = {"glibc": ["2.39-142", "2.39-145"], "macros": ["1-21.el10", "1-22.el10", "1-23.el10"]}
    # the latest version all the architectures having them have
    assert choose_versions({"x86_64_v2": x86, "aarch64": arm}) == {
        "glibc": "2.39-145",
        "lcov": "2.0-5",
        "macros": "1-22.el10",
    }
    # without a version all of them have, the latest one of the first
    assert choose_versions({"x86_64_v2": {"glibc": ["2.39-146"]}, "aarch64": arm}) == {
        "glibc": "2.39-146",
        "macros": "1-23.el10",
    }


def test_lock_sources_locked_meanwhile(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    def versions(_platform: Project, _sources: list[str]) -> dict[str, list[str]]:
        save_lock(project, Lock(sources={"foo": "2-1"}))  # by another command, meanwhile
        return {"foo": ["1-1"], "bar": ["3-1"]}

    monkeypatch.setattr("xcraft.resolution.available_versions", versions)
    lock_sources(project, ["foo", "bar"])
    assert load_lock(project).sources == {"foo": "2-1", "bar": "3-1"}
