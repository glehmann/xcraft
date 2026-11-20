"""The projects: where they are found, their platforms, packages and locks."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from tests import TOML, config
from xcraft.commands import complete_arch, prepared_project
from xcraft.commands.shell import package_of
from xcraft.packages import Package
from xcraft.project import Project, ProjectLockedError


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "xcraft.toml").write_text(TOML)
    return Project(tmp_path, config())


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


def test_sole_use(project: Project) -> None:
    with project.sole_use(), project.sole_use():  # also when this process uses it already
        pass
    fd = os.open(project.root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)  # as another command does
        with pytest.raises(ProjectLockedError, match="other xcraft commands use this project"), project.sole_use():
            pass
    finally:
        os.close(fd)
    with project.sole_use():  # released
        pass


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


def test_unknown_packages_before_preparing(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XCRAFT_PROJECT", str(project.root))
    monkeypatch.setattr("xcraft.resolution.prepare", lambda _: pytest.fail("prepared"))
    with pytest.raises(typer.BadParameter, match="unknown package"):
        prepared_project(["nope"])


def test_package_of_a_path(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("xen", "xen-tools"):
        (project.root / "base" / name / "patches").mkdir(parents=True)
    # by name, or by a path in its directory, like . there, but not a package with its name as prefix
    assert package_of(project, "xen") == "xen"
    monkeypatch.chdir(project.root / "base/xen-tools/patches")
    assert package_of(project, ".") == "xen-tools"
    assert package_of(project, "../../xen") == "xen"
    for given in ("../..", "/"):
        with pytest.raises(typer.BadParameter, match="neither a package nor in the directory of one"):
            package_of(project, given)


def test_specs_parsed_once_by_platform(project: Project) -> None:
    # the project on its platforms shares what it parsed, for each platform
    project.parsed = {"a": Package(name="a")}
    same, other = (p for p in project.all_platforms() if p.platform == project.platform), project.all_platforms()
    assert all(p.parsed is project.parsed for p in same)
    other[1].parsed = {"b": Package(name="b")}
    assert project.all_platforms()[1].parsed is other[1].parsed is not project.parsed
    # but a project found again parses them again, like another command
    assert Project(project.root, config()).specs == {}
