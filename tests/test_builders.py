"""The builders: the builds of an architecture made on a remote host of it, over SSH."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest

from tests import ARCH, cache_image, commit_base, copy_project, edit, git, needs_gpg, xcraft
from xcraft import builders, buildid
from xcraft.cache import build_keys, cached_build
from xcraft.iso import iso_name
from xcraft.project import Project
from xcraft.signing import create_dev_key


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
    # the log of an earlier build there, older than the one here, like of another architecture
    (root / ".xcraft/logs").mkdir(parents=True, exist_ok=True)
    (root / ".xcraft/logs/with-deps.log").write_text("here")
    (copy / ".xcraft/logs").mkdir(parents=True, exist_ok=True)
    (copy / ".xcraft/logs/with-deps.log").write_text("there, before")
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert (root / ".xcraft/logs/with-deps.log").read_text() == "here"
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


@needs_gpg
def should_make_the_iso_on_the_builder_of_the_architecture(
    root: Path, builder: Path, build_environment: Path, tmp_path: Path
) -> None:
    project = Project.find()
    signer = create_dev_key(project, "Developer")
    image = tmp_path / "test.iso"
    image.write_bytes(b"ISO made on the builder")
    # cached there, as its xcraft iso made it
    tree = cache_image(project, image, signer=signer, keep=True)
    copy = builder / builders.ROOT / project.id
    copy_project(copy, build_environment)
    (there := copy / ".xcraft/cache/isos" / project.arch).mkdir(parents=True)
    shutil.move(tree, there / tree.name)
    code, output = xcraft("iso")
    assert code == 0, output
    assert (tree / "image.iso").read_bytes() == image.read_bytes()
    assert (
        root / ".xcraft/iso" / iso_name(project, netinstall=False, signer=signer)
    ).read_bytes() == image.read_bytes()
    # of the developer's key only, like the builds there
    code, output = xcraft("iso", "--final")
    assert code == 2, output
    assert "--final and --key make it here, with --local" in " ".join(output.replace("│", " ").split())


def should_run_a_wheel_of_an_xcraft_installed_from_a_local_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # like by uv tool run --from git+file://..., which the builder cannot reach: a wheel of that commit
    repo = Path(builders.__file__).resolve().parents[1]
    commit = git(repo, "rev-parse", "HEAD").strip()

    class Installed:
        version = "0.1.0"

        @staticmethod
        def read_text(name: str) -> str:
            assert name == "direct_url.json"
            return json.dumps({"url": f"file://{repo}", "vcs_info": {"vcs": "git", "commit_id": commit}})

    monkeypatch.setattr(builders.importlib.metadata, "distribution", lambda _: Installed)
    wheel = builders._source(tmp_path)
    assert isinstance(wheel, Path)
    assert "xcraft/builders.py" in zipfile.ZipFile(wheel).namelist()
    # but the repositories the builder reaches, by their URL
    remote = "https://github.com/glehmann/xcraft"
    monkeypatch.setattr(
        Installed, "read_text", staticmethod(lambda _: json.dumps({"url": remote, "vcs_info": {"commit_id": commit}}))
    )
    assert builders._source(tmp_path / "other") == f"git+{remote}@{commit}"


def should_number_the_builds_of_a_worktree_on_the_builder_against_the_main_branch(
    root: Path, builder: Path, build_environment: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # a worktree, like a workspace of jj, whose .git is a file naming its directory in the repository
    commit_base(root)
    worktree = tmp_path / "worktree"
    git(root, "worktree", "add", "-q", "-b", "feature", str(worktree))
    # relative, like jj's: which the copy on the builder does not resolve
    gitdir = Path((worktree / ".git").read_text().removeprefix("gitdir: ").strip())
    (worktree / ".git").write_text(f"gitdir: {os.path.relpath(gitdir, worktree)}\n")
    shutil.copytree(root / ".xcraft", worktree / ".xcraft", dirs_exist_ok=True)  # its caches
    monkeypatch.setenv("XCRAFT_PROJECT", str(worktree))
    edit(worktree, "changed")
    copy = builder / builders.ROOT / Project.find().id
    copy_project(copy, build_environment)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    # the next number of the main branch's, rather than the first, as without its history
    assert buildid.read(Project.find())["trivial"].number == 2
    assert (copy / ".git").is_dir()


@pytest.mark.usefixtures("builder")
def should_open_the_shell_on_the_builder_of_the_architecture(
    root: Path, builder: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the xcraft of the builder recording its shell, interactive, rather than opening it
    fake = tmp_path / "xcraft-there"
    fake.write_text(
        f'#!/bin/sh\n[ "$1" = shell ] && {{ echo "$PWD $*" > {builder}/shell; exit 0; }}\n'
        f'exec {sys.executable} -m xcraft "$@"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("XCRAFT_BUILDER_XCRAFT", str(fake))
    code, output = xcraft("shell", "trivial")
    assert code == 0, output
    # in the copy of the checkout there, for its build tree, which it keeps, not here
    copy = builder / builders.ROOT / Project.find().id
    assert (builder / "shell").read_text() == f"{copy} shell trivial --arch {ARCH} --local\n"
    assert (copy / "base/trivial/trivial.spec").exists()
    assert not (root / ".xcraft/build/trivial").exists()


def should_show_the_builds_running_on_the_builder_of_the_architecture(root: Path, builder: Path) -> None:
    # the markers of a build there, held by its process
    markers = builder / builders.ROOT / Project.find().id / ".xcraft/build"
    markers.mkdir(parents=True)
    (markers / ".queue").write_text("with-deps")
    holders = [
        subprocess.Popen(["flock", "-o", str(markers / marker), "sleep", "60"]) for marker in ("trivial.turn", ".queue")
    ]
    try:
        for marker in ("trivial.turn", ".queue"):  # once held
            while subprocess.run(["flock", "-n", str(markers / marker), "true"], check=False).returncode == 0:
                time.sleep(0.05)
        code, output = xcraft("status", "--full")
        assert code == 0, output
        assert re.search(r"^trivial +\S+ +building *$", output, re.MULTILINE), output
        assert re.search(r"^with-deps +\S+ +queued 1/1 *$", output, re.MULTILINE), output
    finally:
        for holder in holders:
            holder.kill()
            holder.wait()
    # once done, nothing running there, as without a copy there
    code, output = xcraft("status", "--full")
    assert code == 0, output
    assert "building" not in output
    assert "queued" not in output
    # what its build made, which comes back once all are done, by the keys here
    project = Project.find()
    key = json.loads(xcraft("keys", "trivial")[1])["keys"]["trivial"]
    (builder / builders.ROOT / project.id / cached_build(project, "trivial", key).relative_to(root)).mkdir(parents=True)
    code, output = xcraft("status", "--full")
    assert code == 0, output
    assert re.search(r"^trivial +\S+ +built there *$", output, re.MULTILINE), output
    assert output.splitlines()[-1] == "1 built there, 1 missing"


@pytest.mark.usefixtures("root", "builder")
def should_show_the_status_here_when_the_builder_cannot_be_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUILDER_HOME", "/nonexistent")
    code, output = xcraft("status", "--full")
    assert code == 0, output
    assert "warning: the builds of h are unknown" in output
    assert "2 missing" in output


def should_make_the_developer_key_for_the_builds_of_a_builder_only(root: Path) -> None:
    code, output = xcraft("build", "trivial", "--dry-run")
    assert code == 0, output
    assert not (root / ".xcraft/key").exists()


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
