"""The lock of the builds: xcraft.builds, its format."""

from __future__ import annotations

import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

from tests import TOML, config, git
from xcraft.builds import (
    BuildId,
    Builds,
    builds_path,
    load_builds,
    locked_packages,
    parse_builds,
    recordable,
    save_builds,
)
from xcraft.lock import Recorded
from xcraft.packages import Binary, Package
from xcraft.project import Project

if TYPE_CHECKING:
    from pathlib import Path


def binary(name: str, evr: str = "1:4.2-3.xcpng", arch: str = "x86_64", provides: list[str] | None = None) -> Binary:
    return Binary(name=name, evr=evr, arch=arch, provides=provides or [f"{name} = {evr}"], requires=[])


@pytest.fixture
def project(tmp_path: Path) -> Project:
    (tmp_path / "xcraft.toml").write_text(TOML)
    project = Project(tmp_path, config())
    project.config.platforms = [project.platform]
    project.parsed = {
        "xen": Package(name="xen", binaries=[binary("xen-dom0-libs"), binary("xen-devel")]),
        "lib.c++": Package(name="lib.c++", binaries=[binary("lib.c++")]),
    }
    for name in project.parsed:
        (tmp_path / "base" / name).mkdir(parents=True)
    return project


BUILDS = Builds(
    ids={"xen": BuildId(number=2, key="ab" * 32), "lib.c++": BuildId(number=1, key="cd" * 32, accepts="ef" * 32)},
    buildrequires=Recorded(arches={"aarch64": {"xen": ["meson"]}}),
    requires=Recorded(shared={"xen-dom0-libs": ["lzo", "e2fsprogs-libs"]}, arches={"aarch64": {"lib.c++": []}}),
    provides=Recorded(shared={"xen-devel": ["pkgconfig(xenctrl) = %{version}"]}),
)


def should_save_the_builds_one_line_per_entry(project: Project) -> None:
    save_builds(project, BUILDS)
    # a line by entry, starting with its package, after a comment line with it, and a blank line after them
    assert builds_path(project).read_text() == (
        "# written by xcraft: the builds of our packages, and what they found that the specs do not say\n"
        "\n"
        "# lib.c++\n"
        '"lib.c++".build = 1\n'
        f'"lib.c++".key = "{"cd" * 32}"\n'
        f'"lib.c++".accepts = "{"ef" * 32}"\n'
        '"lib.c++".aarch64.requires."lib.c++" = []\n'
        "\n"
        "# xen\n"
        "xen.build = 2\n"
        f'xen.key = "{"ab" * 32}"\n'
        'xen.requires."xen-dom0-libs" = ["lzo", "e2fsprogs-libs"]\n'
        'xen.provides."xen-devel" = ["pkgconfig(xenctrl) = %{version}"]\n'
        'xen.aarch64.buildrequires = ["meson"]\n'
    )
    assert load_builds(project) == BUILDS
    assert parse_builds("") == Builds()


def should_put_the_versions_back_in_what_the_builds_recorded(project: Project) -> None:
    # with those the specs declare
    devel = locked_packages(project.parsed, BUILDS, project.arch)["xen"].binaries[1]
    assert devel.provides == ["xen-devel = 1:4.2-3.xcpng", "pkgconfig(xenctrl) = 4.2"]


def should_drop_the_records_of_packages_gone(project: Project) -> None:
    del project.parsed["xen"]
    save_builds(project, BUILDS)
    assert load_builds(project) == BUILDS.model_copy(
        update={"requires": Recorded(arches={"aarch64": {"lib.c++": []}}), "provides": Recorded()}
    )


def should_refuse_to_change_the_builds_when_locked(project: Project) -> None:
    save_builds(project, BUILDS)
    project.frozen = True
    save_builds(project, load_builds(project))  # unchanged
    with pytest.raises(RuntimeError, match=r"--locked forbids: .*-xen\.build = 2"):
        save_builds(project, Builds(ids={"lib.c++": BUILDS.ids["lib.c++"]}))


def should_record_only_what_the_spec_does_not_declare() -> None:
    lib = binary("python3-xapi-storage", provides=["python3-xapi-storage = 1:4.2-3.xcpng", "xapi-storage = 1:4.2-3"])
    # not those the spec declares, like its own name; the versions of the package as placeholders
    assert recordable(
        lib,
        [
            "python3-xapi-storage = 1:4.2-3.xcpng",
            "xapi-storage = 1:4.2-3",
            "python3-xapi-storage(x86-64) = 1:4.2-3.xcpng",
            "pkgconfig(xapi) = 4.2",
            "bundled(libfoo) = 0.9",
            "/usr/sbin/sm",
        ],
    ) == [
        "python3-xapi-storage(x86-64) = %{evr}",
        "pkgconfig(xapi) = %{version}",
        "bundled(libfoo) = 0.9",
        "/usr/sbin/sm",
    ]
    # an epoch 0 the same as none, which rpm leaves out of what it generates
    qemu = binary("qemu", evr="0:10.1-1")
    assert recordable(qemu, ["qemu = 10.1-1", "qemu-img = 10.1-1"]) == ["qemu-img = %{evr}"]


def should_show_the_history_of_a_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    git(tmp_path, "init", "-q", "-b", "master")
    for name, message in (("xen", "xen added"), ("xen-tools", "xen-tools added"), ("xen", "xen changed")):
        (tmp_path / "base" / name).mkdir(parents=True, exist_ok=True)
        (tmp_path / "base" / name / f"{name}.spec").write_text(message)
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-qm", message)
    builds = builds_path(Project.find())
    builds.write_text(f'\n# xen\nxen.build = 1\nxen.key = "{"ab" * 32}"\n')
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "xen built")
    builds.write_text(builds.read_text() + f'\n# xen-tools\nxen-tools.build = 1\nxen-tools.key = "{"cd" * 32}"\n')
    git(tmp_path, "commit", "-qam", "xen-tools built")
    # the commits of its directory and of its lines, not those of another package with its name as prefix
    shown = subprocess.run(
        [sys.executable, "-m", "xcraft", "history", "xen"], capture_output=True, text=True, check=True
    ).stdout
    assert [line.strip() for line in shown.splitlines() if line.startswith("    ")] == [
        "xen built",
        "xen changed",
        "xen added",
    ]
