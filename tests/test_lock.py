from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tests import TOML, config
from xcraft.__main__ import app
from xcraft.iso import _lock_tools
from xcraft.lock import Lock, Locked, Recorded, choose_versions, load_lock, lock_path, save_lock, versionlock
from xcraft.project import Project
from xcraft.resolution import Resolved, lock_installed, lock_sources


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
        pending={"xen": {"openssl": "1:3.5.2-1.el10_2"}},
    )
    save_lock(project, lock)
    assert lock_path(project) == tmp_path / "xcraft.lock"
    assert load_lock(project) == lock
    text = lock_path(project).read_text()
    # one line per package, for readable diffs, and the differences of an architecture in its own table
    assert '[sources]\n"glibc" = "2.39-145.el10_2"\n' in text
    assert '[requires.aarch64]\n"qemu" = [\n    "pixman",\n]\n' in text
    assert '[pending."xen"]\n"openssl" = "1:3.5.2-1.el10_2"\n' in text


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


def test_pending() -> None:
    lock = Lock(sources={"glibc": "2.39-1", "openssl": "3.2-1"})
    roots = {"xen": {"glibc", "openssl"}, "vmss": {"glibc"}}

    lock.sources["openssl"] = "3.2-2"
    lock.note_updates({"openssl": "3.2-1"}, roots)
    assert lock.pending == {"xen": {"openssl": "3.2-1"}}

    # the oldest version is kept
    lock.sources |= {"glibc": "2.39-2", "openssl": "3.2-3"}
    lock.note_updates({"glibc": "2.39-1", "openssl": "3.2-2"}, roots)
    assert lock.pending == {"xen": {"openssl": "3.2-1", "glibc": "2.39-1"}, "vmss": {"glibc": "2.39-1"}}

    # back to the version it was built with
    lock.sources["glibc"] = "2.39-1"
    lock.note_updates({"glibc": "2.39-2"}, roots)
    assert lock.pending == {"xen": {"openssl": "3.2-1"}}

    lock.clear_pending(["xen"], ["glibc"])
    assert lock.pending == {"xen": {"openssl": "3.2-1"}}
    lock.clear_pending(["xen"])
    assert lock.pending == {}


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


def test_ack(project: Project) -> None:
    pending = {"xen": {"glibc": "2.39-1", "openssl": "3.2-1"}, "vmss": {"glibc": "2.39-1"}}
    save_lock(project, Lock(sources={"glibc": "2.39-2", "openssl": "3.2-2"}, pending=pending))
    result = CliRunner().invoke(app, ["ack", "--dep", "glibc"])
    assert result.exit_code == 0, result.output
    assert result.output == "vmss: glibc\nxen: glibc\n"
    assert load_lock(project).pending == {"xen": {"openssl": "3.2-1"}}
    assert CliRunner().invoke(app, ["ack", "nope"]).exit_code == 2
    assert CliRunner().invoke(app, ["ack", "xen"]).exit_code == 0  # not in the project, with pending updates
    assert load_lock(project).pending == {}


def test_iso_tools(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    packages = {"bash": locked("5.2-3", source="bash"), "xorriso": locked("1.5.6-1", source="xorriso")}
    resolved = Resolved(image=["bash"], packages=packages)
    for module in ("xcraft.iso", "xcraft.resolution"):
        monkeypatch.setattr(f"{module}.resolve_image", lambda _: resolved)
    locked_sources = []
    monkeypatch.setattr("xcraft.resolution.lock_sources", lambda _, sources: locked_sources.extend(sources))
    save_lock(project, Lock(sources={"bash": "5.2-3"}))
    assert _lock_tools(project, packages)
    assert (load_lock(project).iso, locked_sources) == ({project.arch: ["xorriso"]}, ["xorriso"])
    with pytest.raises(RuntimeError, match="installed in another version than locked: xorriso"):
        _lock_tools(project, {"xorriso": locked("1.5.6-2", source="xorriso")})


def test_update(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    save_lock(project, Lock(sources={"glibc": "2.39-142", "sg3_utils": "1.48-7.el10_2.1"}))
    available = {
        "x86_64_v2": {"glibc": ["2.39-142", "2.39-145", "2.39-146"], "sg3_utils": ["1.48-7.el10_2.1"]},
        "aarch64": {"glibc": ["2.39-142", "2.39-145"], "sg3_utils": ["1.48-7.el10_2.1"]},
    }
    looked = []

    def versions(platform: Project, sources: list[str]) -> dict[str, list[str]]:
        looked.append((platform.arch, sources))
        return {s: v for s, v in available[platform.arch].items() if s in sources}

    monkeypatch.setattr("xcraft.commands.lock.available_versions", versions)
    # the build roots before the update, for the pending updates of all the architectures
    roots = {
        "x86_64_v2": Resolved(packages={"glibc": locked("2.39-142", source="glibc")}, buildroots={"xen": ["glibc"]}),
        "aarch64": Resolved(packages={"glibc": locked("2.39-142", source="glibc")}, buildroots={"vmss": ["glibc"]}),
    }

    def prepare(_platform: Project, *, roots: bool = True) -> None:
        if roots:  # which locks the sources the build roots need
            lock = load_lock(project)
            lock.sources["lzo"] = "2.10-1"
            save_lock(project, lock)

    monkeypatch.setattr("xcraft.commands.lock.prepare", prepare)
    monkeypatch.setattr("xcraft.resolution.prepare", prepare)
    monkeypatch.setattr("xcraft.commands.lock.resolve", lambda platform: roots[platform.arch])
    result = CliRunner().invoke(app, ["lock", "--outdated"])
    assert (result.exit_code, result.output) == (
        1,
        "glibc 2.39-142 -> 2.39-145\n1 to update: xcraft lock --update updates them\n",
    )
    assert load_lock(project).sources["glibc"] == "2.39-142"
    # only the given ones, binary or source names
    monkeypatch.setattr(
        "xcraft.resolution.resolve_image", lambda _: Resolved(packages={"sg3_utils-libs": locked("1", "sg3_utils")})
    )
    looked.clear()
    assert CliRunner().invoke(app, ["lock", "--outdated", "sg3_utils-libs"]).output == "up to date\n"
    assert looked == [("x86_64_v2", ["sg3_utils"]), ("aarch64", ["sg3_utils"])]
    assert "not locked: nope" in str(CliRunner().invoke(app, ["lock", "--update", "nope"]).exception)
    result = CliRunner().invoke(app, ["lock", "--update"])
    assert (result.exit_code, result.output) == (0, "glibc 2.39-142 -> 2.39-145\n1 updated, see xcraft impacts\n")
    lock = load_lock(project)
    assert lock.sources["lzo"] == "2.10-1"
    assert (lock.sources["glibc"], lock.pending) == (
        "2.39-145",
        {"vmss": {"glibc": "2.39-142"}, "xen": {"glibc": "2.39-142"}},
    )


def test_lock_installed_in_other_versions(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    # installed in their latest version, but locked in the one all the architectures have
    resolved = [Resolved(), Resolved(packages={"foo": locked("1-1", source="foo")})]
    monkeypatch.setattr("xcraft.resolution.resolve_image", lambda _: resolved[0])
    monkeypatch.setattr("xcraft.resolution.lock_sources", lambda *_: resolved.pop(0))
    assert not lock_installed(project, {"foo": locked("2-1", source="foo")})
    save_lock(project, Lock(sources={"foo": "1-1"}))  # as lock_sources() does
    assert lock_installed(project, {"foo": locked("1-1", source="foo")})


def test_lock_sources_locked_meanwhile(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    def versions(_platform: Project, _sources: list[str]) -> dict[str, list[str]]:
        save_lock(project, Lock(sources={"foo": "2-1"}))  # by another command, meanwhile
        return {"foo": ["1-1"], "bar": ["3-1"]}

    monkeypatch.setattr("xcraft.resolution.available_versions", versions)
    lock_sources(project, ["foo", "bar"])
    assert load_lock(project).sources == {"foo": "2-1", "bar": "3-1"}


def test_lock_installed_binaries_of_other_versions(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    # foo-new, of the version of foo installed, not of the one locked
    resolved = [Resolved(), Resolved(packages={"foo": locked("1-1", source="foo")})]
    monkeypatch.setattr("xcraft.resolution.resolve_image", lambda _: resolved[0])

    def locking(*_: object) -> None:
        resolved.pop(0)
        save_lock(project, Lock(sources={"foo": "1-1"}))

    monkeypatch.setattr("xcraft.resolution.lock_sources", locking)
    assert not lock_installed(project, {"foo-new": locked("2-1", source="foo")})
