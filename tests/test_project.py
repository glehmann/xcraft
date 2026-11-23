"""The projects: where they are found, their platforms, packages and locks."""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import typer
from pydantic import ValidationError

from tests import TOML, config, xcraft
from xcraft.commands import complete_arch
from xcraft.commands.shell import package_of
from xcraft.packages import Package
from xcraft.project import Config, Project, ProjectLockedError, Publish


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "xcraft.toml").write_text(TOML)
    return Project(tmp_path, config())


def should_let_one_command_build_at_a_time(project: Project) -> None:
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


def should_refuse_sole_use_when_other_commands_use_the_project(project: Project) -> None:
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


def should_complete_the_packages_while_cleaning(project: Project) -> None:
    (project.root / "base/foo").mkdir(parents=True)
    env = {**os.environ, "XCRAFT_PROJECT": str(project.root), "_XCRAFT_COMPLETE": "complete_bash"}
    env |= {"COMP_WORDS": "xcraft build f", "COMP_CWORD": "2"}
    fd = os.open(project.root, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # as xcraft clean does
        # not waiting for it: the timeout only stops a completion that waits
        completed = subprocess.run(
            [Path(sys.executable).parent / "xcraft"], env=env, capture_output=True, text=True, timeout=30, check=True
        )
    finally:
        os.close(fd)
    assert completed.stdout.split() == ["foo"]


def should_refuse_a_package_in_two_repositories(project: Project) -> None:
    project.config.repositories = ["base", "xostor"]
    for path in ("base/foo", "base/.foo.import", "xostor/bar", "iso/bar"):
        (project.root / path).mkdir(parents=True)
    assert project.package_dirs == {"bar": project.root / "xostor/bar", "foo": project.root / "base/foo"}
    (project.root / "xostor/foo").mkdir()
    with pytest.raises(RuntimeError, match="foo is in two repositories: base and xostor"):
        _ = Project(project.root, project.config).package_dirs


def should_find_the_project_in_the_parent_directories(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


# run from outside the projects, from one, or with $XCRAFT_PROJECT
@pytest.mark.parametrize(
    ("cwd", "override", "found"), [(".", None, "installed"), ("cwd", None, "cwd"), (".", "override", "override")]
)
def should_find_the_project_of_the_installed_executable(
    tmp_path: Path, cwd: str, override: str | None, found: str
) -> None:
    executable = tmp_path / "installed/.venv/bin/xcraft"
    executable.parent.mkdir(parents=True)
    executable.write_text("from xcraft.project import Project\nprint(Project.find().root)\n")
    for name in ("installed", "cwd", "override"):
        (tmp_path / name).mkdir(exist_ok=True)
        (tmp_path / name / "xcraft.toml").write_text(TOML)
    env = {k: v for k, v in os.environ.items() if k != "XCRAFT_PROJECT"}
    env |= {"XCRAFT_PROJECT": str(tmp_path / override)} if override else {}
    result = subprocess.run(
        [sys.executable, str(executable)], cwd=tmp_path / cwd, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(tmp_path / found)


def should_build_for_the_host_architecture_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def should_complete_the_architectures_of_the_project(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XCRAFT_PROJECT", str(project.root))
    assert (complete_arch(""), complete_arch("a")) == (["x86_64_v2", "aarch64"], ["aarch64"])


def should_use_the_repositories_of_each_platform(project: Project) -> None:
    x86, arm = project.all_platforms()
    assert ("epel" in x86.repos, "epel-fedora" in x86.repos) == (True, False)
    assert ("epel" in arm.repos, "epel-fedora" in arm.repos) == (False, True)


def should_refuse_unknown_packages_before_preparing(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XCRAFT_PROJECT", str(project.root))
    code, output = xcraft("build", "nope")
    assert code == 2, output
    assert "unknown package(s): nope" in output
    assert not (project.root / "xcraft.lock").exists()


def should_find_the_package_of_a_path(project: Project, monkeypatch: pytest.MonkeyPatch) -> None:
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


def should_parse_the_specs_once_per_platform(project: Project) -> None:
    # the project on its platforms shares what it parsed, for each platform
    project.parsed = {"a": Package(name="a")}
    x86, arm = project.all_platforms()
    arm.parsed = {"b": Package(name="b")}
    assert (x86.parsed, project.all_platforms()[1].parsed, project.parsed) == (
        {"a": Package(name="a")},
        {"b": Package(name="b")},
        {"a": Package(name="a")},
    )


def should_require_the_digest_of_the_base_image() -> None:
    # a tag only may name another image later, while the build keys stay the same
    with pytest.raises(ValidationError, match=r"base_image\n.*@sha256:"):
        Config.model_validate({**tomllib.loads(TOML), "base_image": "ghcr.io/almalinux/10-base:10.2"})


def should_require_a_platform() -> None:
    with pytest.raises(ValidationError, match=r"platforms\n.*at least 1 item"):
        Config.model_validate({**tomllib.loads(TOML), "platforms": []})


def should_refuse_embargo_branches_that_are_not_maintenance_ones() -> None:
    with pytest.raises(ValidationError, match="embargo_branches must be maintenance_branches too: other/"):
        Config.model_validate(
            {**tomllib.loads(TOML), "maintenance_branches": ["maint/*", "embargo/*"], "embargo_branches": ["other/*"]}
        )


def should_reject_stages_that_are_not_plain_names(unprepared_root: Path) -> None:
    for stage in ("", ".", "..", "../other", "/absolute", "a/b"):
        with pytest.raises(ValidationError):
            Publish(stage=stage)
    code, output = xcraft("publish", "repos", "--local", "--stage", "../other")
    assert code == 2
    assert "stage must start" in " ".join(output.split())
