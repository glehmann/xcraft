"""The builders: the builds of an architecture made on a remote host of it, over SSH."""

from __future__ import annotations

import os
import shutil
import sys
import zipfile
from typing import TYPE_CHECKING

import pytest

from tests import ARCH, copy_project, xcraft
from xcraft import builders
from xcraft.cache import build_keys, cached_build
from xcraft.project import Project

if TYPE_CHECKING:
    from pathlib import Path

# ssh, rsync and uv reaching the builder h, a directory of this machine standing for its home: no remote host in the
# tests
SSH = """#!/bin/sh
while [ $# -gt 0 ]; do case "$1" in -o|-p|-O) shift 2;; -*) shift;; *) break;; esac; done
shift
[ $# -eq 0 ] && exit 0
cd "$BUILDER_HOME" && PATH="${BUILDER_PATH:-$PATH}" exec /bin/sh -c "$*"
"""
RSYNC = """#!/bin/sh
for arg in "$@"; do
    shift
    case "$arg" in h:*) set -- "$@" "$BUILDER_HOME/${arg#h:}";; *) set -- "$@" "$arg";; esac
done
exec {rsync} "$@"
"""
# uv tool run --from <wheel> xcraft on the builder: the xcraft of this process, once the wheel there, without installing
# it; uv otherwise
UV = """#!/bin/sh
[ "$1 $2 $3 $4" = "--quiet tool run --from" ] || exec {uv} "$@"
[ "$6" = xcraft ] && [ -f "$5" ] || { echo "unexpected: uv $*" >&2; exit 1; }
shift 6
exec {python} -m xcraft "$@"
"""


@pytest.fixture
def builder(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return the home of h, the builder of the architecture of the tests, for the project `root`."""
    tools, home = tmp_path / "tools", tmp_path / "builder"
    tools.mkdir()
    home.mkdir()
    rsync, uv = shutil.which("rsync"), shutil.which("uv")
    assert rsync, "the builders need rsync"
    assert uv, "the development versions of xcraft need uv"
    for name, script in (
        ("ssh", SSH),
        ("rsync", RSYNC.replace("{rsync}", rsync)),
        ("uv", UV.replace("{uv}", uv).replace("{python}", sys.executable)),
    ):
        (tools / name).write_text(script)
        (tools / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
    monkeypatch.setenv("BUILDER_HOME", str(home))
    monkeypatch.setenv("XCRAFT_BUILDERS", f"{ARCH}=h")
    monkeypatch.delenv("XCRAFT_BUILDER_XCRAFT", raising=False)
    return home


def should_take_the_builders_of_the_config_overridden_by_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("XCRAFT_BUILDERS", raising=False)
    assert builders.builders() == {}
    (tmp_path / "xcraft").mkdir()
    (tmp_path / "xcraft/config.toml").write_text('[builders]\naarch64 = "ssh://arm64.example"\nx86_64_v2 = "x86"\n')
    assert builders.builders() == {"aarch64": "ssh://arm64.example", "x86_64_v2": "x86"}
    # overridden by the environment, none for an empty host
    monkeypatch.setenv("XCRAFT_BUILDERS", "x86_64_v2=,aarch64=h")
    assert builders.builders() == {"aarch64": "h"}
    monkeypatch.setenv("XCRAFT_BUILDERS", "aarch64")
    with pytest.raises(ValueError, match="not <arch>=<host>: aarch64"):
        builders.builders()


def should_build_on_the_builder_of_the_architecture_with_this_xcraft(
    root: Path, builder: Path, build_environment: Path
) -> None:
    # the builder having the caches of the build environment already, like after its first build
    copy = builder / builders.ROOT / Project.find().id
    copy_project(copy, build_environment)
    (copy / "base/trivial/trivial.spec").unlink()  # the copy brings it, as the rest of the checkout
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert (copy / "base/trivial/trivial.spec").read_text() == (root / "base/trivial/trivial.spec").read_text()
    # run from a wheel of this xcraft, a development version, with the files of the build environment
    wheels = list(builder.glob(f"{builders.ROOT}/wheels/*/*.whl"))
    assert len(wheels) == 1, wheels
    assert "xcraft/buildenv/build.sh" in zipfile.ZipFile(wheels[0]).namelist()
    # built there, cached here, under its key, with its log
    project = Project.find()
    assert cached_build(project, "trivial", build_keys(project)["trivial"]).exists()
    assert (root / ".xcraft/logs/trivial.log").exists()
    code, output = xcraft("status", "--full", "trivial")
    assert code == 0, output
    assert " ready" in output


@pytest.mark.usefixtures("builder")
def should_make_the_final_builds_here_only() -> None:
    code, output = xcraft("build", "trivial", "--final")
    assert code == 2, output
    assert "--final and --key build here, with --local" in " ".join(output.replace("│", " ").split())


def should_check_the_tools_of_the_builder_before_copying(
    builder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # git above all, without which its builds would be final ones
    lacking = tmp_path / "lacking"
    lacking.mkdir()
    for tool in ("rsync", "flock"):
        (lacking / tool).symlink_to(shutil.which(tool) or tool)
    monkeypatch.setenv("BUILDER_PATH", str(lacking))
    # an xcraft installed there, rather than uv, which the builders find in ~/.local/bin too, of this machine here
    monkeypatch.setenv("XCRAFT_BUILDER_XCRAFT", "xcraft")
    code, output = xcraft("build", "trivial")
    assert code == 1, output
    assert "the builder h lacks podman, gpg, git: install them there" in output
    assert not (builder / builders.ROOT).exists()


@pytest.mark.usefixtures("builder")
def should_refuse_a_builder_running_another_xcraft(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # of another recipe of the builds, which would make builds of other keys: refused before building
    other = tmp_path / "other-xcraft"
    other.write_text('#!/bin/sh\necho \'{"recipe": "0123456789abcdef"}\'\n')
    other.chmod(0o755)
    monkeypatch.setenv("XCRAFT_BUILDER_XCRAFT", str(other))
    code, output = xcraft("build", "trivial")
    assert code == 1, output
    assert "h runs another xcraft, of the recipe 0123456789ab, rather than" in output


def should_report_the_builders_and_what_they_lack_in_doctor(
    builder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert f"✓ builder of {ARCH}: h\n" in xcraft("doctor")[1]
    lacking = tmp_path / "lacking"
    lacking.mkdir()
    for tool in ("rsync", "flock"):
        (lacking / tool).symlink_to(shutil.which(tool) or tool)
    monkeypatch.setenv("BUILDER_PATH", str(lacking))
    monkeypatch.setenv("XCRAFT_BUILDER_XCRAFT", "xcraft")  # rather than uv, see the tools check
    code, output = xcraft("doctor")
    assert code == 1, output
    assert (
        f"✗ builder of {ARCH}: the builder h lacks podman, gpg, git: install them there, or see xcraft doctor --fix"
        in (" ".join(output.split()))
    )


def should_check_only_what_a_builder_needs_on_a_builder() -> None:
    output = xcraft("doctor", "--builder")[1]
    # neither the tools of the other commands, nor emulation
    assert [line.split()[1].rstrip(":") for line in output.splitlines()] == ["podman", "gpg", "git", "rsync", "flock"]


@pytest.mark.usefixtures("builder")
def should_provision_a_builder_by_the_doctor_of_this_xcraft_there(capfd: pytest.CaptureFixture[str]) -> None:
    builders.provision("h")
    assert "✓ flock: " in capfd.readouterr().out
