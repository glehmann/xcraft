"""Build IDs: the numbers of the builds, in .buildid/, and the development builds."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests import copy_project
from tests.test_build import xcraft
from xcraft import buildid, buildkey
from xcraft.iso import build_iso
from xcraft.lock import load_lock, save_lock
from xcraft.project import Project

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_environment: None) -> Path:
    root = tmp_path / "project"
    copy_project(root)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


def test_recipe_of_all_architectures(root: Path) -> None:
    # the repositories of the build environment differ by architecture, not the build keys
    projects = [Project.find(arch) for arch in ("x86_64_v2", "aarch64")]
    assert projects[0].repos.keys() != projects[1].repos.keys()
    assert buildkey.recipe(projects[0]) == buildkey.recipe(projects[1])


def test_build_ids_of_all_architectures(root: Path) -> None:
    # with-deps built against trivial on aarch64 only: %ifarch is noarch for noarch packages
    spec = root / "base/with-deps/with-deps.spec"
    text = spec.read_text().replace("BuildArch: noarch\n", "")
    spec.write_text(text.replace("BuildRequires: trivial\n", "%ifarch aarch64\nBuildRequires: trivial\n%endif\n"))
    projects = [Project.find(arch) for arch in ("x86_64_v2", "aarch64")]
    assert projects[0].deps["with-deps"] != projects[1].deps["with-deps"]
    assert buildid.compute(projects[0], None) == buildid.compute(projects[1], None)


def git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def commit_base(root: Path, branch: str = "master") -> None:
    """Make `root` a git repository, whose main branch, `branch`, has the build IDs."""
    assert git(root, "init", "-q", "-b", branch).returncode == 0
    update()
    git(root, "add", "-A")
    git(root, "commit", "-qm", "base")


def update() -> dict[str, int]:
    """Update the build IDs, and return their numbers."""
    project = Project.find()
    buildid.update(project)
    return {name: i.number for name, i in buildid.read(project).items()}


def edit(root: Path, line: str, *, name: str = "trivial", top: bool = True) -> None:
    spec = root / f"base/{name}/{name}.spec"
    spec.write_text(f"# {line}\n{spec.read_text()}" if top else f"{spec.read_text()}# {line}\n")


def built(*args: str) -> list[str]:
    """Build, and return the RPMs of the development builds made."""
    code, output = xcraft("build", *args)
    assert code == 0, output
    return [line.rsplit("/", 1)[-1] for line in output.splitlines() if line.endswith(".rpm")]


def test_build_ids(root: Path) -> None:
    code, output = xcraft("build")
    assert code == 0, output
    assert "build IDs: trivial 1, with-deps 1" in output
    ids = buildid.read(Project.find())
    assert (root / ".buildid/trivial").read_text() == f"1 {ids['trivial'].key}\n"
    assert {p.name for p in root.glob(".xcraft/cache/builds/*/trivial/*/RPMS/*/*.rpm")} == {
        "trivial-1.0-1+b1.xcpng8.99.noarch.rpm"
    }
    # unchanged
    code, output = xcraft("build")
    assert code == 0, output
    assert "build IDs" not in output
    assert output.splitlines()[-1] == "2 cached"
    # out of git, without the main branch to number against, a change keeps the numbers, with other keys
    edit(root, "changed")
    assert update() == {"trivial": 1, "with-deps": 1}
    assert all(i.key != ids[name].key for name, i in buildid.read(Project.find()).items())
    edit(root, "changed again")
    project = Project.find()
    project.frozen = True
    with pytest.raises(buildid.BuildIdError, match="--locked forbids: trivial, with-deps"):
        buildid.update(project)


def test_build_key_inputs(root: Path) -> None:
    def key() -> str:
        return buildid.compute(Project.find(), None)["trivial"].key

    before = key()
    config = root / "xcraft.toml"
    text = config.read_text()
    # not what does not change the builds: the compiler caches, the remote storage, the publication
    unrelated = (
        '[ccache_size]\ntrivial = "1G"\n[remote.main]\nurl = "https://example.com"\n[publish]\nstage = "other"\n'
    )
    config.write_text(f"{text}\n{unrelated}")
    assert key() == before
    # but the recipe, like the macros of the builds
    config.write_text(text.replace('xcpng = "9"', 'xcpng = "10"'))
    assert key() != before
    config.write_text(text)
    # the package directory
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"{spec.read_text()}# changed\n")
    assert key() != before
    spec.write_text(spec.read_text().removesuffix("# changed\n"))
    assert key() == before
    # and the lock, on each architecture: the versions of the image
    inputs = buildkey.Inputs(Project.find())
    assert buildkey.key(inputs.of({"trivial"}, {}), "trivial") == before
    for arch in ("x86_64_v2", "aarch64"):
        source = inputs.lock.image[arch][0]
        version = inputs.lock.sources[source]
        inputs.lock.sources[source] = "0-0"
        assert buildkey.key(inputs.of({"trivial"}, {}), "trivial") != before
        inputs.lock.sources[source] = version


def test_shown_inputs(root: Path) -> None:
    update()
    code, output = xcraft("status", "trivial", "with-deps", "--inputs")
    assert code == 0, output
    shown = json.loads(output)
    for name in ("trivial", "with-deps"):
        # the key, as docs/build-keys.md specifies it: of its inputs and its name, that of its build ID
        inputs = json.dumps(shown[name]["inputs"], sort_keys=True)
        key = hashlib.sha256(f"{inputs}\0{name}".encode()).hexdigest()
        assert shown[name]["key"] == key
        assert (root / ".buildid" / name).read_text() == f"{shown[name]['build ID']}\n"
        assert shown[name]["build ID"].split()[1] == key
    # with-deps is built against trivial: its inputs have its build ID
    package, recipe, lock, deps = shown["with-deps"]["inputs"]["with-deps"]
    assert deps == [["trivial", 1, shown["trivial"]["key"]]]
    assert recipe == shown["trivial"]["inputs"]["trivial"][1]
    assert sorted(lock) == ["aarch64", "x86_64_v2"]
    assert package != shown["trivial"]["inputs"]["trivial"][0]


def test_build_ids_checked_by_all_the_builds(root: Path) -> None:
    assert xcraft("build")[0] == 0  # the lock complete, with what the builds record
    edit(root, "changed")  # without its build ID
    code, output = xcraft("build", "--locked")
    assert code == 1
    assert "--locked forbids: trivial, with-deps" in output
    # those of the ISOs too
    project = Project.find()
    project.frozen = True
    with pytest.raises(buildid.BuildIdError, match="--locked forbids: trivial, with-deps"):
        build_iso(project)


def test_build_ids_of_main_branch(root: Path) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    # the same number however many times the branch changes, to the package and those built against it
    edit(root, "feature", top=False)
    assert update() == {"trivial": 2, "with-deps": 2}
    key = buildid.read(Project.find())["trivial"].key
    edit(root, "feature again", top=False)
    assert update() == {"trivial": 2, "with-deps": 2}
    assert buildid.read(Project.find())["trivial"].key != key
    git(root, "commit", "-qam", "feature")
    # another branch merged first: 2 is taken
    git(root, "checkout", "-q", "master")
    edit(root, "master")
    assert update() == {"trivial": 2, "with-deps": 2}
    git(root, "commit", "-qam", "master")
    git(root, "checkout", "-q", "feature")
    # not until merged: the branch is numbered against its fork only
    assert update() == {"trivial": 2, "with-deps": 2}
    assert git(root, "merge", "-q", "master").returncode != 0
    assert "<<<<<<<" in (root / ".buildid/trivial").read_text()
    assert update() == {"trivial": 3, "with-deps": 3}
    # back to the main branch's packages: its build IDs
    git(root, "checkout", "-q", "master", "--", "base")
    update()
    assert git(root, "diff", "--quiet", "master", "--", ".buildid").returncode == 0


def test_main_branch_setting(root: Path) -> None:
    (root / "xcraft.toml").write_text(f'main_branch = "main"\n{(root / "xcraft.toml").read_text()}')
    commit_base(root, "main")
    git(root, "checkout", "-qb", "feature")
    edit(root, "feature")
    assert update() == {"trivial": 2, "with-deps": 2}
    edit(root, "feature again")
    assert update() == {"trivial": 2, "with-deps": 2}


def test_development_builds(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a main branch with what the builds record in its lock, like those CI checks
    assert xcraft("build")[0] == 0
    commit_base(root)
    shutil.rmtree(root / ".xcraft/cache/builds")
    # on the main branch, final builds only
    assert built() == []
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed", name="with-deps")
    # only the changed package, trivial is the main branch's build
    assert built() == ["with-deps-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm"]
    assert built() == []  # the same inputs: cached
    assert re.search(r"^with-deps +1\.0-1\+b2~1\.dev\.xcpng8\.99 +built", xcraft("status")[1], re.MULTILINE)
    assert re.search(r"^trivial +1\.0-1\+b1\.xcpng8\.99 +built", xcraft("status")[1], re.MULTILINE)
    edit(root, "changed again", name="with-deps")
    assert re.search(r"^with-deps +1\.0-1\+b2~2\.dev\.xcpng8\.99 +outdated", xcraft("status")[1], re.MULTILINE)
    assert built() == ["with-deps-1.0-1+b2~2.dev.xcpng8.99.noarch.rpm"]
    # another identifier: the counter goes on, for the builds to upgrade the previous ones
    assert built("--dev", "qcow2") == ["with-deps-1.0-1+b2~3.qcow2.xcpng8.99.noarch.rpm"]
    monkeypatch.setenv("XCRAFT_DEV", "qcow2")
    assert built() == []
    # final builds, even with $XCRAFT_DEV, but not with --dev
    assert built("--final") == []
    assert "with-deps-1.0-1+b2.xcpng8.99.noarch.rpm" in {
        p.name for p in root.glob(".xcraft/cache/builds/*/with-deps/*/RPMS/*/*.rpm")
    }
    code, output = xcraft("build", "--final", "--dev", "x")
    assert code == 2
    assert "--final and --dev cannot be used together" in output
    # the repository of the development builds of the given packages, for test hosts
    code, output = xcraft("repo", "with-deps", "--baseurl", "http://example.com/qcow2/")
    assert code == 0, output
    repo = root / "repo-qcow2"
    assert sorted(p.name for p in repo.glob("*.rpm")) == ["with-deps-1.0-1+b2~3.qcow2.xcpng8.99.noarch.rpm"]
    assert "baseurl=http://example.com/qcow2/\n" in (repo / "xcraft-qcow2.repo").read_text()
    assert output.splitlines()[-2:] == [
        "on the hosts, with this directory at http://example.com/qcow2/:",
        "  copy xcraft-qcow2.repo to /etc/yum.repos.d/, then: dnf upgrade --refresh with-deps",
    ]
    # the development builds, as last built, are kept
    assert xcraft("clean")[0] == 0
    assert built() == []


def test_development_builds_changed_by_others(root: Path) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    # another release: what rpm generated for trivial, which with-deps is built against, changes with its build
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("Release: 1%{?dist}", "Release: 2%{?dist}"))
    assert built() == [
        "trivial-1.0-2+b2~1.dev.xcpng8.99.noarch.rpm",
        "with-deps-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm",
    ]
    assert re.search(r"^with-deps +1\.0-1\+b2~1\.dev\.xcpng8\.99 +built", xcraft("status")[1], re.MULTILINE)
    assert built() == []
    # without what the builds recorded: the cached builds record it again, before the keys of those built against them
    git(root, "checkout", "--", "xcraft.lock")
    assert xcraft("build", "--dry-run") == (0, "0 to build, 2 cached\n")
    assert built() == []


def test_dynamic_buildrequires_changing_the_build_number(root: Path) -> None:
    # a dynamic BuildRequires the build environment has already, which the lock of the main branch misses: its build
    # records it, which changes its key, and its number
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("%prep", "%generate_buildrequires\necho coreutils\n\n%prep"))
    commit_base(root)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    # cached under the release suffix of its RPMs, after its key
    rpms = list(root.glob(".xcraft/cache/builds/*/trivial/*/RPMS/*/*.rpm"))
    assert rpms
    for rpm in rpms:
        assert rpm.parents[2].name[64:] in rpm.name


def test_numbers_given_again_on_main_branch(root: Path) -> None:
    commit_base(root)
    edit(root, "changed")
    assert update() == {"trivial": 2, "with-deps": 2}
    git(root, "commit", "-qam", "changed")
    project = Project.find()
    project.frozen = True
    buildid.update(project)
    # a merge conflict resolved by keeping one side: the number of the previous commit, for another key
    edit(root, "other change")
    git(root, "commit", "-qam", "other change")
    ids = buildid.compute(project, buildid.base(project))
    assert ids["trivial"].number == 3
    ids["trivial"] = ids["trivial"].model_copy(update={"number": 2})
    with pytest.raises(buildid.BuildIdError, match=r"given again to other builds, since the previous commit: trivial$"):
        buildid.check_first_parent(project, ids)
    ids["trivial"] = ids["trivial"].model_copy(update={"number": 3})
    buildid.check_first_parent(project, ids)


def test_reused_builds(root: Path) -> None:
    code, output = xcraft("reuse", "with-deps")
    assert code == 1
    assert "reusing builds needs the main branch, master, in a git repository" in output
    assert xcraft("build")[0] == 0  # the builds of the main branch
    commit_base(root)
    base = buildid.read(Project.find())
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed")
    assert update() == {"trivial": 2, "with-deps": 2}
    # with-deps, built against trivial, reuses its build, accepting the change
    code, output = xcraft("reuse", "with-deps")
    assert code == 0, output
    assert output.splitlines()[-1] == "with-deps: reuses its build 1"
    deps = buildid.read(Project.find())["with-deps"]
    assert (deps.number, deps.key) == (1, base["with-deps"].key)
    assert deps.accepts
    assert (root / ".buildid/with-deps").read_text() == f"1 {deps.key} accepts {deps.accepts}\n"
    # kept while the key is the accepted one, committed or not: a main branch's build, not a development one
    assert update() == {"trivial": 2, "with-deps": 1}
    git(root, "commit", "-qam", "reused")
    assert update() == {"trivial": 2, "with-deps": 1}
    assert built("with-deps") == ["trivial-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm"]
    assert "with-deps-1.0-1+b1.xcpng8.99.noarch.rpm" in {
        p.name for p in root.glob(".xcraft/cache/builds/*/with-deps/*/RPMS/*/*.rpm")
    }
    # until it changes otherwise
    edit(root, "changed again")
    assert update() == {"trivial": 2, "with-deps": 2}
    assert not buildid.read(Project.find())["with-deps"].accepts
    # a package changed itself is not reused
    code, output = xcraft("reuse", "trivial")
    assert code == 0, output
    assert output.splitlines()[-1] == "trivial: changed itself, built again"


def test_reused_builds_of_upstream_updates(root: Path) -> None:
    # a script run by toyrun, upstream, which rpm generates a requirement on: with-deps is built against toyrun
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace('echo "hello" > hello.txt', 'printf "#!/usr/bin/toyrun\\necho hello\\n" > hello.sh')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "install -m 755 %{name}-%{version}/hello.sh %{buildroot}/usr/share/%{name}/")
        .replace("/usr/share/%{name}/hello.txt", "/usr/share/%{name}/hello.sh")
    )  # fmt: skip
    lock = load_lock(Project.find())
    lock.sources["toyrun"] = "1-1"  # locked before its update
    save_lock(Project.find(), lock)
    assert xcraft("build", "with-deps")[0] == 0
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    assert xcraft("lock", "--update", "toyrun")[0] == 0
    assert update() == {"trivial": 1, "with-deps": 2}
    # not for other updates
    code, output = xcraft("reuse", "--dep", "trivial-upstream")
    assert code == 1
    assert "not locked: trivial-upstream" in output
    code, output = xcraft("reuse", "--dep", "glibc")
    assert code == 0, output
    assert update() == {"trivial": 1, "with-deps": 2}
    code, output = xcraft("reuse", "--dep", "toyrun")
    assert code == 0, output
    assert output.splitlines()[-1] == "with-deps: reuses its build 1"
    assert update() == {"trivial": 1, "with-deps": 1}
