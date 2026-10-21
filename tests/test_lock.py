from __future__ import annotations

import pytest
from typer.testing import CliRunner

from xcraft.__main__ import app
from xcraft.iso import _lock_tools
from xcraft.lock import Lock, Locked, load_lock, lock_path, save_lock
from xcraft.project import Config, Project


def locked(evr: str, source: str = "foo") -> Locked:
    return Locked(evr=evr, arch="x86_64_v2", source=source, repo="appstream", sha256="ab" * 32)


def test_save_and_load(tmp_path) -> None:  # noqa: ANN001
    project = Project(tmp_path, Config())
    lock = Lock(image=["bash"], packages={"gcc-c++": locked("1:14.2-1"), "bash": locked("5.2-3")})
    lock.buildroots = {"xen": ["gcc-c++"]}
    lock.iso = ["gcc-c++"]
    save_lock(project, lock)
    assert lock_path(project) == tmp_path / "locks/x86_64_v2.toml"
    assert load_lock(project) == lock
    # one line per package, for readable diffs
    assert '"gcc-c++" = { evr = "1:14.2-1", arch = "x86_64_v2", source = "foo"' in lock_path(project).read_text()


def test_versionlock() -> None:
    lock = Lock(packages={"bash": locked("5.2-3"), "gcc": locked("1:14.2-1")})
    assert lock.versionlock() == "bash-0:5.2-3.x86_64_v2\ngcc-1:14.2-1.x86_64_v2\n"
    assert lock.versionlock(["gcc"]) == "gcc-1:14.2-1.x86_64_v2\n"


def test_merge() -> None:
    lock = Lock(packages={"bash": locked("5.2-3")})
    other_repo = locked("5.2-3").model_copy(update={"repo": "devel"})
    assert lock.merge({"bash": other_repo, "zlib": locked("1.3-1")}) == ["zlib"]
    assert lock.packages["bash"].repo == "appstream"
    with pytest.raises(RuntimeError, match="another version than locked: bash"):
        lock.merge({"bash": locked("5.2-4")})


def test_pending() -> None:
    lock = Lock(image=["glibc"], packages={"glibc": locked("2.39-1"), "openssl-devel": locked("3.2-1")})
    lock.buildroots = {"xen": ["openssl-devel"], "vmss": []}

    lock.packages["openssl-devel"] = locked("3.2-2")
    lock.note_updates({"openssl-devel": "3.2-1"})
    assert lock.pending == {"xen": {"openssl-devel": "3.2-1"}}

    # the image is in every build root; the oldest version is kept
    lock.packages["glibc"] = locked("2.39-2")
    lock.packages["openssl-devel"] = locked("3.2-3")
    lock.note_updates({"glibc": "2.39-1", "openssl-devel": "3.2-2"})
    assert lock.pending == {"xen": {"openssl-devel": "3.2-1", "glibc": "2.39-1"}, "vmss": {"glibc": "2.39-1"}}

    # back to the version it was built with
    lock.packages["glibc"] = locked("2.39-1")
    lock.note_updates({"glibc": "2.39-2"})
    assert lock.pending == {"xen": {"openssl-devel": "3.2-1"}}

    lock.clear_pending(["xen"], ["glibc"])
    assert lock.pending == {"xen": {"openssl-devel": "3.2-1"}}
    lock.clear_pending(["xen"])
    assert lock.pending == {}


def test_pending_saved(tmp_path) -> None:  # noqa: ANN001
    project = Project(tmp_path, Config())
    lock = Lock(packages={"gcc-c++": locked("1:14.2-1")}, pending={"xen": {"gcc-c++": "1:14.1-3"}})
    save_lock(project, lock)
    assert load_lock(project) == lock
    assert '[pending."xen"]\n"gcc-c++" = "1:14.1-3"\n' in lock_path(project).read_text()


def test_ack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    (tmp_path / "xcraft.toml").write_text("")
    project = Project(tmp_path, Config())
    pending = {"xen": {"glibc": "2.39-1", "openssl-devel": "3.2-1"}, "vmss": {"glibc": "2.39-1"}}
    save_lock(project, Lock(pending=pending))
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    result = CliRunner().invoke(app, ["ack", "--dep", "glibc"])
    assert result.exit_code == 0, result.output
    assert result.output == "vmss: glibc\nxen: glibc\n"
    assert load_lock(project).pending == {"xen": {"openssl-devel": "3.2-1"}}
    assert CliRunner().invoke(app, ["ack", "xen"]).exit_code == 0
    assert load_lock(project).pending == {}


def test_iso_tools(tmp_path) -> None:  # noqa: ANN001
    project = Project(tmp_path, Config())
    save_lock(project, Lock(image=["bash"], packages={"bash": locked("5.2-3")}))
    tools = {"bash": locked("5.2-3"), "xorriso": locked("1.5.6-1")}
    _lock_tools(project, tools)
    lock = load_lock(project)
    assert lock.iso == ["xorriso"]
    assert lock.packages["xorriso"] == tools["xorriso"]


def test_frozen(tmp_path) -> None:  # noqa: ANN001
    project = Project(tmp_path, Config())
    lock = Lock(image=["bash"], packages={"bash": locked("5.2-3")}, buildroots={"xen": ["bash"]})
    save_lock(project, lock)
    project.frozen = True
    save_lock(project, lock)  # unchanged
    lock.merge({"zlib": locked("1.3-1")})
    lock.buildroots["xen"] = ["zlib"]
    with pytest.raises(
        RuntimeError,
        match=r"--locked forbids: \+buildroots\.xen zlib, \+packages zlib-1\.3-1\.x86_64_v2, "
        r"-buildroots\.xen bash$",
    ):
        save_lock(project, lock)
    assert "zlib" not in load_lock(project).packages
