from __future__ import annotations

import pytest
from typer.testing import CliRunner

from tests import TOML, config
from xcraft.__main__ import app
from xcraft.iso import _lock_tools
from xcraft.lock import Available, Lock, Locked, choose_versions, differences, load_lock, lock_path, save_lock
from xcraft.project import Project


def locked(evr: str, source: str = "foo", arch: str = "x86_64_v2") -> Locked:
    return Locked(evr=evr, arch=arch, source=source, repo="appstream", sha256="ab" * 32)


def test_save_and_load(tmp_path) -> None:  # noqa: ANN001
    project = Project(tmp_path, config())
    lock = Lock(image=["bash"], packages={"gcc-c++": locked("1:14.2-1"), "bash": locked("5.2-3")})
    lock.buildroots = {"xen": ["gcc-c++"]}
    lock.iso = ["gcc-c++"]
    save_lock(project, lock)
    assert lock_path(project) == tmp_path / "xcraft.x86_64_v2.lock"
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
    project = Project(tmp_path, config())
    lock = Lock(packages={"gcc-c++": locked("1:14.2-1")}, pending={"xen": {"gcc-c++": "1:14.1-3"}})
    save_lock(project, lock)
    assert load_lock(project) == lock
    assert '[pending."xen"]\n"gcc-c++" = "1:14.1-3"\n' in lock_path(project).read_text()


def test_ack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    (tmp_path / "xcraft.toml").write_text(TOML)
    project = Project(tmp_path, config())
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
    project = Project(tmp_path, config())
    save_lock(project, Lock(image=["bash"], packages={"bash": locked("5.2-3")}))
    tools = {"bash": locked("5.2-3"), "xorriso": locked("1.5.6-1")}
    _lock_tools(project, tools)
    lock = load_lock(project)
    assert lock.iso == ["xorriso"]
    assert lock.packages["xorriso"] == tools["xorriso"]


def test_frozen(tmp_path) -> None:  # noqa: ANN001
    project = Project(tmp_path, config())
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


SUFFIXES = [".alma_altarch"]


def architectures() -> dict[str, Lock]:
    """Return the locks of two architectures, one with AlmaLinux's rebuilds of EPEL."""
    return {
        "x86_64_v2": Lock(
            packages={"glibc": locked("2.39-142"), "macros": locked("1-21.el10.alma_altarch", arch="noarch")}
        ),
        "aarch64": Lock(
            packages={"glibc": locked("2.39-145", arch="aarch64"), "macros": locked("1-21.el10", arch="noarch")}
        ),
    }


def test_choose_versions() -> None:
    locks = architectures()
    x86 = {
        "glibc": [locked("2.39-142"), locked("2.39-145"), locked("2.39-146"), locked("2.39-146", arch="x86_64")],
        "macros": [locked(f"1-{r}.el10.alma_altarch", arch="noarch") for r in (21, 22)],
    }
    arm = {
        "glibc": [locked(f"2.39-{r}", arch="aarch64") for r in (142, 145)],
        "macros": [locked(f"1-{r}.el10", arch="noarch") for r in (21, 22, 23)],
    }
    # the latest version all the architectures have, but the suffixes of the rebuilds
    chosen = choose_versions(locks, {"x86_64_v2": x86, "aarch64": arm}, SUFFIXES)
    assert {arch: {n: p.evr for n, p in c.items()} for arch, c in chosen.items()} == {
        "x86_64_v2": {"glibc": "2.39-145", "macros": "1-22.el10.alma_altarch"},
        "aarch64": {"glibc": "2.39-145", "macros": "1-22.el10"},
    }
    # an architecture alone follows the others
    chosen = choose_versions(locks, {"aarch64": arm}, SUFFIXES)["aarch64"]
    assert {n: p.evr for n, p in chosen.items()} == {"glibc": "2.39-142", "macros": "1-21.el10"}
    # without a version all of them have, the latest one of each
    chosen = choose_versions(locks, {"x86_64_v2": {"glibc": x86["glibc"][2:]}, "aarch64": arm}, SUFFIXES)
    assert (chosen["x86_64_v2"]["glibc"].evr, chosen["aarch64"]["glibc"].evr) == ("2.39-146", "2.39-145")


def test_differences() -> None:
    assert differences(architectures(), SUFFIXES) == {"glibc": {"x86_64_v2": "2.39-142", "aarch64": "2.39-145"}}
    assert set(differences(architectures(), [])) == {"glibc", "macros"}


def test_check(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    (tmp_path / "xcraft.toml").write_text(f'release_suffixes = [".alma_altarch"]\n{TOML}')
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    monkeypatch.delenv("XCRAFT_ARCH", raising=False)
    assert "no lock for aarch64 yet" in CliRunner().invoke(app, ["lock", "--arch", "aarch64"]).output
    x86, arm = Project.find().all_platforms()
    for platform, lock in zip((x86, arm), architectures().values(), strict=True):
        save_lock(platform, lock)
    result = CliRunner().invoke(app, ["lock", "--check"])
    assert result.exit_code == 1
    assert "glibc 2.39-142 on x86_64_v2, 2.39-145 on aarch64\n1 packages locked in other versions" in result.output
    save_lock(arm, Lock(packages={"glibc": locked("2.39-142", arch="aarch64")}))
    assert CliRunner().invoke(app, ["lock", "--check"]).exit_code == 0


def test_update_from_available_files(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    (tmp_path / "xcraft.toml").write_text(f'release_suffixes = [".alma_altarch"]\n{TOML}')
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    monkeypatch.delenv("XCRAFT_ARCH", raising=False)
    # the files of CI, from a machine of each architecture: no container here
    monkeypatch.setattr("xcraft.commands.lock.available_versions", lambda *_: pytest.fail("looked for the versions"))
    x86, arm = Project.find().all_platforms()
    for platform, lock in zip((x86, arm), architectures().values(), strict=True):
        save_lock(platform, lock)
    files = [tmp_path / "x86.json", tmp_path / "arm.json"]
    files[0].write_text(
        Available(arch="x86_64_v2", versions={"glibc": [locked("2.39-142"), locked("2.39-145")]}).model_dump_json()
    )
    files[1].write_text(
        Available(arch="aarch64", versions={"glibc": [locked("2.39-145", arch="aarch64")]}).model_dump_json()
    )
    options = [arg for f in files for arg in ("--available", str(f))]
    result = CliRunner().invoke(app, ["lock", "--outdated", *options])
    assert (result.exit_code, result.output) == (
        1,
        "glibc 2.39-142 -> 2.39-145 on x86_64_v2\n1 to update: xcraft lock --update updates them\n",
    )
    result = CliRunner().invoke(app, ["lock", "--update", *options])
    assert result.exit_code == 0, result.output
    assert (load_lock(x86).packages["glibc"].evr, load_lock(arm).packages["glibc"].evr) == ("2.39-145", "2.39-145")
    files[1].write_text(Available(arch="ppc64le", versions={}).model_dump_json())
    assert (
        "no lock for the architectures of --available: ppc64le"
        in CliRunner().invoke(app, ["lock", "--update", *options]).output
    )


def test_update_of_one_architecture_only(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    (tmp_path / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    monkeypatch.delenv("XCRAFT_ARCH", raising=False)
    x86, arm = Project.find().all_platforms()
    save_lock(x86, Lock(packages={"sg3_utils": locked("1.48-7.el10_2.1")}))
    save_lock(arm, Lock(packages={"glibc": locked("2.39-145", arch="aarch64")}))
    looked = []

    def versions(project: Project, names: list[str]) -> dict[str, list[Locked]]:
        looked.append(project.arch)
        return {"sg3_utils": [locked("1.48-7.el10_2.1"), locked("1.48-7.el10_2.2")]}

    monkeypatch.setattr("xcraft.commands.lock.available_versions", versions)
    # locked on x86_64_v2 only: not an unknown package, nor a container for aarch64
    result = CliRunner().invoke(app, ["lock", "--update", "sg3_utils"])
    assert result.exit_code == 0, result.output
    assert (looked, load_lock(x86).packages["sg3_utils"].evr) == (["x86_64_v2"], "1.48-7.el10_2.2")
    assert "not locked: nope" in CliRunner().invoke(app, ["lock", "--update", "nope"]).output


def test_binaries() -> None:
    lock = Lock(
        packages={"sg3_utils": locked("1-1", source="sg3_utils"), "sg3_utils-libs": locked("1-1", source="sg3_utils")}
    )
    assert lock.binaries(["sg3_utils"]) == ["sg3_utils", "sg3_utils-libs"]
    assert lock.binaries(["sg3_utils-libs", "gone"]) == ["gone", "sg3_utils-libs"]
    assert (lock.source("sg3_utils-libs"), lock.source("gone")) == ("sg3_utils", "gone")
