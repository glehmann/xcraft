"""Build IDs: the numbers of the builds, in xcraft.builds, and the development builds."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from typing import TYPE_CHECKING

import pytest

from tests import ARCH, TOML, commit_base, config, edit, git, ours, xcraft
from xcraft import build as builder
from xcraft import buildid, buildkey, devbuilds
from xcraft.builds import BuildId, builds_path, load_builds, save_builds
from xcraft.cache import build_keys
from xcraft.iso import checkout_iso, dev_suffixes, iso_key, iso_name
from xcraft.lock import Lock, load_lock, save_lock
from xcraft.project import Project

if TYPE_CHECKING:
    from pathlib import Path


def should_give_the_same_build_ids_on_all_architectures(root: Path) -> None:
    # with-deps built against trivial on aarch64 only: %ifarch is noarch for noarch packages
    spec = root / "base/with-deps/with-deps.spec"
    text = spec.read_text().replace("BuildArch: noarch\n", "")
    spec.write_text(text.replace("BuildRequires: trivial\n", "%ifarch aarch64\nBuildRequires: trivial\n%endif\n"))
    projects = [Project.find(arch) for arch in ("x86_64_v2", "aarch64")]
    assert projects[0].deps["with-deps"] != projects[1].deps["with-deps"]
    assert (ids := buildid.compute(projects[0], None)) == buildid.compute(projects[1], None)
    # and the cache keys of their development builds, built against them on any architecture
    autorev = {"trivial": "~1.dev", "with-deps": "~1.dev"}
    assert build_keys(projects[0], autorev, ids=ids) == build_keys(projects[1], autorev, ids=ids)


def update() -> dict[str, int | str]:
    """Update the build IDs, and return their numbers, like 2, or "1.1" for maintenance ones."""
    project = Project.find()
    buildid.update(project)
    return {name: i.build for name, i in buildid.read(project).items()}


def _development_rpms() -> set[Path]:
    """Return the RPMs of the development builds of the cache, in <key>+b<n>~<c>.<id>/RPMS/<arch>/."""
    return {p for p in (Project.find().state / "cache/builds").rglob("*.rpm") if "~" in p.parents[2].name}


def built(*args: str) -> list[str]:
    """Build, and return the RPMs of the development builds made: those new in the cache."""
    before = _development_rpms()
    code, output = xcraft("build", *args)
    assert code == 0, output
    return sorted(p.name for p in _development_rpms() - before)


def should_reject_malformed_build_ids() -> None:
    key = "ab" * 32
    # keys of 64 hexadecimal digits only, which the release suffixes follow in the caches
    for invalid in ({"key": "abc"}, {"key": f"{key}0"}, {"accepts": key[1:]}):
        with pytest.raises(ValueError, match=r"key|accepts"):
            BuildId.model_validate({"number": 1, "key": key} | invalid)
    with pytest.raises(ValueError, match="number"):
        BuildId(number=0, key=key)  # the release suffix +b0, before the first build


def should_forget_the_build_ids_in_merge_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    ids = {"bar": BuildId(number=1, key="ab" * 32), "foo": BuildId(number=2, key="cd" * 32)}
    path = builds_path(Project.find())
    save_builds(Project.find(), load_builds(Project.find()).model_copy(update={"ids": ids}))
    text = path.read_text()
    # a merge conflict of git, or of jj, which the build IDs given again resolve, keeping the other packages
    git_conflict = "<<<<<<< ours\nfoo.build = 2\n=======\nfoo.build = 3\n>>>>>>> theirs\n"
    jj_conflict = (
        "<<<<<<< conflict 1 of 1\n%%%%%%% diff\n-foo.build = 1\n+foo.build = 2\n+++++++ side\nfoo.build = 3\n"
        ">>>>>>> end\n"
    )
    for conflict in (git_conflict, jj_conflict):
        path.write_text(text.replace("foo.build = 2\n", conflict))
        assert buildid.read(Project.find()) == {"bar": ids["bar"]}


def should_number_the_builds_until_locked(root: Path) -> None:
    (root / "xcraft.toml").write_text(f'{TOML}installer = ["bash"]\n')  # for the ISO
    code, output = xcraft("build")
    assert code == 0, output
    assert "build IDs: trivial 1, with-deps 1" in output
    ids = buildid.read(Project.find())
    assert (
        f'\n# trivial\ntrivial.build = 1\ntrivial.key = "{ids["trivial"].key}"\n'
        in builds_path(Project.find()).read_text()
    )
    assert {p.name for p in root.glob(".xcraft/cache/builds/*/*/trivial/*/RPMS/*/*.rpm")} == {
        "trivial-1.0-1+b1.xcpng8.99.noarch.rpm"
    }
    # out of git, without the main branch to number against, a change keeps the numbers, with other keys
    edit(root, "changed")
    assert update() == {"trivial": 1, "with-deps": 1}
    assert all(i.key != ids[name].key for name, i in buildid.read(Project.find()).items())
    # checked by all the builds, of the ISOs too, of x86_64, whatever the host
    edit(root, "changed again")
    code, output = xcraft("build", "--locked")
    assert code == 1
    assert "--locked forbids: trivial, with-deps" in output
    project = Project.find("x86_64_v2")
    project.frozen = True
    with pytest.raises(buildid.BuildIdError, match="--locked forbids: trivial, with-deps"):
        checkout_iso(project)


def should_show_the_inputs_of_the_build_keys(root: Path) -> None:
    update()
    code, output = xcraft("status", "trivial", "with-deps", "--inputs")
    assert code == 0, output
    shown = json.loads(output)
    for name in ("trivial", "with-deps"):
        # the key, as docs/build-keys.md specifies it: of its inputs and its name, that of its build ID
        inputs = json.dumps(shown[name]["inputs"], sort_keys=True)
        key = hashlib.sha256(f"{inputs}\0{name}".encode()).hexdigest()
        assert shown[name]["key"] == key
        assert shown[name]["build ID"] == buildid.read(Project.find())[name].text()
        assert shown[name]["build ID"].split()[1] == key
        # with the image of the machine the specs are parsed in, which the keys do not cover
        assert shown[name]["specs parsed in"].startswith("localhost/xcraft-build:")
    # with-deps is built against trivial: its inputs have its build ID
    package, recipe, lock, deps = shown["with-deps"]["inputs"]["with-deps"]
    assert deps == [["trivial", 1, shown["trivial"]["key"]]]
    assert recipe == shown["trivial"]["inputs"]["trivial"][1]
    assert sorted(lock) == ["aarch64", "x86_64_v2"]
    assert package != shown["trivial"]["inputs"]["trivial"][0]


def should_number_branches_against_the_main_branch(root: Path) -> None:
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
    with pytest.raises(subprocess.CalledProcessError):
        git(root, "merge", "-q", "master")
    assert "<<<<<<<" in (root / "xcraft.builds").read_text()
    assert update() == {"trivial": 3, "with-deps": 3}
    # back to the main branch's packages: its build IDs
    git(root, "checkout", "-q", "master", "--", "base")
    update()
    assert git(root, "diff", "master", "--", "xcraft.builds") == ""


def should_number_against_the_configured_main_branch(root: Path) -> None:
    (root / "xcraft.toml").write_text(f'main_branch = "main"\n{(root / "xcraft.toml").read_text()}')
    commit_base(root, "main")
    git(root, "checkout", "-qb", "feature")
    edit(root, "feature")
    assert update() == {"trivial": 2, "with-deps": 2}
    edit(root, "feature again")
    assert update() == {"trivial": 2, "with-deps": 2}


def locked() -> None:
    """Check the build IDs as CI does, with --locked."""
    project = Project.find()
    project.frozen = True
    buildid.update(project)


def version(name: str) -> str:
    """Return the version of the build of `name` xcraft status shows."""
    return next(line.split()[1] for line in xcraft("status", "--full")[1].splitlines() if line.split()[0] == name)


def should_number_maintenance_fixes_between_releases(root: Path) -> None:
    commit_base(root)
    release = git(root, "rev-parse", "HEAD")
    edit(root, "master", name="with-deps")
    assert update() == {"trivial": 1, "with-deps": 2}
    git(root, "commit", "-qam", "master")
    # a fix of the release, on a maintenance branch: above the release's numbers, below master's next ones
    # made at the release, master having moved since: its branches are maintenance ones
    git(root, "checkout", "-qb", "maint/9.0", release)
    git(root, "checkout", "-qb", "fix")
    edit(root, "fix")
    assert update() == {"trivial": "1.1", "with-deps": "1.1"}
    assert version("trivial") == "1.0-1+b1.1~1.dev.xcpng8.99"
    git(root, "commit", "-qam", "fix")
    git(root, "checkout", "-q", "maint/9.0")
    git(root, "merge", "-q", "--no-ff", "fix")
    # final builds there, checked as on master
    locked()
    assert version("trivial") == "1.0-1+b1.1.xcpng8.99"
    # the fix revised, on another branch of it
    git(root, "checkout", "-qb", "revised")
    edit(root, "revised")
    assert update() == {"trivial": "1.2", "with-deps": "1.2"}
    assert version("with-deps") == "1.0-1+b1.2~1.dev.xcpng8.99"
    git(root, "commit", "-qam", "revised")
    git(root, "checkout", "-q", "maint/9.0")
    git(root, "merge", "-q", "--no-ff", "revised")
    locked()
    # merged into master: trivial keeps its build, master changed none of its inputs; with-deps, which master changed,
    # gets master's next number
    git(root, "checkout", "-q", "master")
    with pytest.raises(subprocess.CalledProcessError):
        git(root, "merge", "-q", "--no-ff", "maint/9.0")
    assert update() == {"trivial": "1.2", "with-deps": 3}
    assert version("trivial") == "1.0-1+b1.2.xcpng8.99"
    git(root, "commit", "-qam", "merge")
    locked()
    # and the next one, after
    edit(root, "after the merge")
    assert update() == {"trivial": 2, "with-deps": 4}
    # still maintenance commits, the branch removed, with the release tag of its last release
    git(root, "tag", "release/9.0/stable/1", "maint/9.0")
    git(root, "branch", "-qD", "maint/9.0")
    git(root, "stash", "-q")
    git(root, "checkout", "-q", "release/9.0/stable/1")
    locked()
    assert version("trivial") == "1.0-1+b1.2.xcpng8.99"


def should_identify_the_commit_of_the_checkout(root: Path) -> None:
    project = Project.find()
    assert buildid.commit(project) is None  # out of git
    (root / ".gitignore").write_text(".xcraft/\n")
    commit_base(root)
    head = git(root, "rev-parse", "HEAD")
    assert buildid.commit(project) == head
    edit(root, "changed")
    assert buildid.commit(project) == f"{head}-dirty"


@pytest.mark.skipif(not shutil.which("jj"), reason="needs jj")
def should_identify_the_commit_of_the_working_copy_of_jj(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the commit of its working copy, which git sees as changes, or its parent's while it is empty
    project = Project.find()
    (settings := tmp_path / "jj.toml").write_text('user.name = "test"\nuser.email = "test@example.com"\n')
    monkeypatch.setenv("JJ_CONFIG", str(settings))
    (root / ".gitignore").write_text(".xcraft/\n")
    commit_base(root)
    head = git(root, "rev-parse", "HEAD")
    subprocess.run(["jj", "git", "init", "--colocate"], cwd=root, capture_output=True, check=True)
    assert buildid.commit(project) == head
    edit(root, "changed with jj")
    working = buildid.commit(project)
    assert working not in (head, f"{head}-dirty")
    log = ["jj", "log", "-r", "@", "--no-graph", "-T", "commit_id"]
    assert working == subprocess.run(log, cwd=root, capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(not shutil.which("jj"), reason="needs jj")
def should_number_jj_maintenance_bookmarks_like_branches(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (settings := tmp_path / "jj.toml").write_text('user.name = "test"\nuser.email = "test@example.com"\n')
    monkeypatch.setenv("JJ_CONFIG", str(settings))

    def jj(*args: str) -> None:
        subprocess.run(["jj", *args], cwd=root, capture_output=True, check=True)

    (root / ".gitignore").write_text(".xcraft/\n")
    commit_base(root)
    jj("git", "init", "--colocate")
    # editing the revision of the bookmark of a maintenance branch, which git sees as changes on master
    jj("bookmark", "create", "maint/xsa-123", "-r", "@")
    edit(root, "fix")
    assert update() == {"trivial": "1.1", "with-deps": "1.1"}
    assert version("trivial") == "1.0-1+b1.1~1.dev.xcpng8.99"
    # on top of it, like jj new maint/xsa-123 makes: the next build of the branch, whose tip has 1.1
    jj("new")
    edit(root, "fix again")
    assert update() == {"trivial": "1.2", "with-deps": "1.2"}
    assert version("trivial") == "1.0-1+b1.2~1.dev.xcpng8.99"
    # not on a maintenance branch anymore
    jj("bookmark", "delete", "maint/xsa-123")
    assert update() == {"trivial": 2, "with-deps": 2}


def should_number_a_fix_after_its_target_when_master_did_not_move(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "maint/9.0")
    git(root, "checkout", "-qb", "fix")
    edit(root, "fix")
    # master still at the release: the branch is told a maintenance one by its target only, like in CI
    assert update() == {"trivial": 2, "with-deps": 2}
    monkeypatch.setenv("XCRAFT_MAINTENANCE", "nothing")
    with pytest.raises(buildid.BuildIdError, match="no maintenance branch nothing, of maint/"):
        update()
    monkeypatch.setenv("XCRAFT_MAINTENANCE", "maint/9.0")
    assert update() == {"trivial": "1.1", "with-deps": "1.1"}
    monkeypatch.setenv("XCRAFT_MAINTENANCE", "master")
    assert update() == {"trivial": 2, "with-deps": 2}
    assert xcraft("--maintenance", "maint/9.0", "build", "--ids")[0] == 0
    assert buildid.read(Project.find())["trivial"].build == "1.1"


def should_refuse_maintenance_numbers_merged_by_fast_forward(root: Path) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "maint/9.0")
    git(root, "checkout", "-qb", "fix")
    edit(root, "fix")
    assert xcraft("--maintenance", "maint/9.0", "build", "--ids")[0] == 0
    git(root, "commit", "-qam", "fix")
    git(root, "checkout", "-q", "maint/9.0")
    git(root, "merge", "-q", "--no-ff", "fix")
    # its commits on master's first-parent chain, with numbers master never gives
    git(root, "checkout", "-q", "master")
    git(root, "merge", "-q", "--ff-only", "maint/9.0")
    with pytest.raises(
        buildid.BuildIdError, match=r"not from the maintenance branches it merges: trivial, with-deps; "
    ):
        locked()


def should_skip_spec_tests_only_for_development_builds(root: Path) -> None:
    trivial, with_deps = (root / f"base/{n}/{n}.spec" for n in ("trivial", "with-deps"))
    trivial.write_text(trivial.read_text().replace("%files\n", "%check\necho trivial-checked\n\n%files\n"))
    assert xcraft("build")[0] == 0
    commit_base(root)
    shutil.rmtree(root / ".xcraft/cache/builds")
    git(root, "checkout", "-qb", "feature")
    with_deps.write_text(
        with_deps.read_text().replace("%files\n", "%check\necho with-deps-checked\nexit 1\n\n%files\n")
    )
    code, output = xcraft("build", "with-deps", "--nocheck")
    assert code == 0, output
    # the final build of trivial, which the remote storage misses, with its tests, unlike the development one
    (final,) = root.glob(f".xcraft/cache/builds/*/{ARCH}/trivial/*")
    assert "trivial-checked" in (final / "build.log").read_text()
    (development,) = root.glob(f".xcraft/cache/builds/*/{ARCH}/with-deps/*")
    assert "~1.dev" in development.name
    assert "with-deps-checked" not in (development / "build.log").read_text()
    assert xcraft("build", "with-deps", "--nocheck") == (0, "2 cached\n")
    # the builds with the tests make it again, which fails, keeping it for those without
    assert xcraft("build", "with-deps", "-n")[1].startswith("build    with-deps")
    code, output = xcraft("build", "with-deps")
    assert code == 1
    assert "✗ with-deps" in output, output
    assert xcraft("build", "with-deps", "--nocheck") == (0, "2 cached\n")


def should_refuse_development_build_options_that_conflict(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    code, output = xcraft("build", "--final", "--dev", "x")
    assert code == 2
    assert "--final and --dev cannot be used together" in output
    code, output = xcraft("build", "--nocheck", "--final")
    assert code == 2
    assert "--final and --nocheck cannot be used together" in " ".join(output.replace("│", "").split())
    monkeypatch.setenv("XCRAFT_DEV_COUNTER", "-1")
    code, output = xcraft("build", "--dev", "pr7")
    assert code != 0
    assert "$XCRAFT_DEV_COUNTER must be a positive number: -1" in output


@pytest.mark.parametrize(
    ("cycle", "alone"), [({"a": {"b"}, "b": {"a"}}, "a"), ({"a": {"b", "c"}, "b": {"a"}, "c": set()}, "c")]
)
def should_number_the_development_builds_of_a_cycle_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cycle: dict[str, set[str]], alone: str
) -> None:
    # a cycle of the dependencies of all the architectures, and maybe a package it is built against, all developed: new
    # on a branch
    (tmp_path / "xcraft.toml").write_text(TOML)
    git(tmp_path, "init", "-q", "-b", "master")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "base")
    git(tmp_path, "checkout", "-qb", "feature")
    project = Project(tmp_path, config())
    project.config.platforms = [project.platform]  # the one of the dependencies given
    ours(project, monkeypatch, cycle)
    save_lock(project, Lock())
    hashes = dict.fromkeys(cycle, "1" * 64)

    def build(ident: str, names: list[str]) -> dict[str, str]:
        suffixes = devbuilds.dev_keys(project, ident, hashes)[1]
        devbuilds.keep_built(project, ident, suffixes, names, hashes)
        return suffixes

    assert build("dev", list(cycle)) == dict.fromkeys(cycle, "~1.dev")
    # another development build of a, or of c, which a is built against, alone: b, built against a, is not
    build("other", [alone])
    # but its next one, of other inputs, has a newer release, like a
    again = build("dev", list(cycle))
    assert again["b"] == again["a"] != "~1.dev"
    assert build("dev", list(cycle)) == again


def compression(root: Path, rpm: str) -> str:
    """Return the zstd level of the payload of the cached `rpm`, with T when compressed by several threads."""
    (path,) = root.glob(f".xcraft/cache/builds/*/*/*/*/RPMS/*/{rpm}")
    query = ["rpm", "-qp", "--qf", "%{PAYLOADFLAGS}", path]
    return subprocess.run(query, capture_output=True, text=True, check=True).stdout


@pytest.mark.skipif(not shutil.which("rpm"), reason="needs rpm")
def should_make_development_builds_of_the_changed_packages(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: PLR0915
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
    # compressed faster than the final builds, which may be pushed
    assert compression(root, "with-deps-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm") == "3T"
    assert built() == []  # the same inputs: cached
    assert re.search(r"^with-deps +1\.0-1\+b2~1\.dev\.xcpng8\.99 +ready", xcraft("status", "--full")[1], re.MULTILINE)
    assert re.search(r"^trivial +1\.0-1\+b1\.xcpng8\.99 +ready", xcraft("status", "--full")[1], re.MULTILINE)
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
        p.name for p in root.glob(".xcraft/cache/builds/*/*/with-deps/*/RPMS/*/*.rpm")
    }
    assert compression(root, "with-deps-1.0-1+b2.xcpng8.99.noarch.rpm") == "19"
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
    # copied to a host, with the main branch's builds of the given packages too, which it may have in other builds:
    # by ssh and rsync doing nothing, without a host to copy to
    tools = root / "tools"
    tools.mkdir()
    for tool in ("ssh", "rsync"):
        (tools / tool).write_text("#!/bin/sh\n")
        (tools / tool).chmod(0o755)
    with monkeypatch.context() as patch:
        patch.setenv("PATH", f"{tools}:{os.environ['PATH']}")
        code, output = xcraft("repo", "with-deps", "trivial", "--host", "h")
    assert code == 0, output
    assert "warning: no development builds of trivial: the main branch's ones" in output
    assert sorted(p.name for p in repo.glob("*.rpm")) == [
        "trivial-1.0-1+b1.xcpng8.99.noarch.rpm",
        "with-deps-1.0-1+b2~3.qcow2.xcpng8.99.noarch.rpm",
    ]
    assert "repository on h, in /var/local/xcraft/qcow2" in output
    # the development builds, as last built, are kept
    assert xcraft("clean")[0] == 0
    assert built() == []
    # what to install them with, rather than the paths of their RPMs, but with --verbose
    edit(root, "changed once more", name="with-deps")
    code, output = xcraft("build")
    assert code == 0, output
    assert "hint: xcraft repo with-deps makes a repository of these development builds, for the hosts" in output
    assert ".rpm" not in output
    edit(root, "changed for the last time", name="with-deps")
    code, output = xcraft("build", "--verbose")
    assert code == 0, output
    assert re.search(r"^/.*/RPMS/noarch/with-deps-1\.0-1\+b2~5\.qcow2\.xcpng8\.99\.noarch\.rpm$", output, re.MULTILINE)
    # numbered by CI, by the run number of a workflow, rather than the counters of .xcraft, which it does not keep
    edit(root, "changed in CI", name="with-deps")
    monkeypatch.setenv("XCRAFT_DEV_COUNTER", "42")
    assert built("--dev", "pr7") == ["with-deps-1.0-1+b2~42.pr7.xcpng8.99.noarch.rpm"]


def should_name_the_isos_after_the_development_builds_of_a_branch(root: Path) -> None:
    def key(dev: str | None) -> str:
        return iso_key(Project.find(), netinstall=False, volid=None, signer=None, dev=dev)

    # on the main branch, of final builds only, like the remote storage's
    commit_base(root)
    assert dev_suffixes(Project.find(), "dev") == {}
    assert key("dev") == key(None)
    # on another branch, of the development builds of the packages changed, like xcraft build makes them
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed", name="with-deps")
    assert dev_suffixes(Project.find(), "dev") == {"with-deps": "~1.dev"}
    assert len({key("dev"), key("other"), key(None)}) == 3
    name = iso_name(Project.find(), netinstall=False, signer=None, dev="dev")
    assert name.endswith(f".dev-{ARCH}.iso")
    assert name.removesuffix(f".dev-{ARCH}.iso") + f"-{ARCH}.iso" == iso_name(
        Project.find(), netinstall=False, signer=None
    )


def should_rebuild_dependents_against_other_development_builds(root: Path) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed")
    edit(root, "changed", name="with-deps")
    assert built("--dev", "dev") == [
        "trivial-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm",
        "with-deps-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm",
    ]
    assert built("trivial", "--dev", "other") == ["trivial-1.0-1+b2~2.other.xcpng8.99.noarch.rpm"]
    # with-deps built again against another trivial: of a newer release, for the hosts to upgrade it
    assert built("--dev", "dev") == [
        "trivial-1.0-1+b2~3.dev.xcpng8.99.noarch.rpm",
        "with-deps-1.0-1+b2~2.dev.xcpng8.99.noarch.rpm",
    ]
    assert built("--dev", "dev") == []


def should_number_dependents_again_when_their_dependencies_change(root: Path) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    # another release: what rpm generated for trivial, which with-deps is built against, changes with its build; the
    # builds of the new release are numbered from 1, those of with-deps, of the same one, from the next number
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("Release: 1%{?dist}", "Release: 2%{?dist}"))
    assert built() == [
        "trivial-1.0-2+b1~1.dev.xcpng8.99.noarch.rpm",
        "with-deps-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm",
    ]
    assert re.search(r"^with-deps +1\.0-1\+b2~1\.dev\.xcpng8\.99 +ready", xcraft("status", "--full")[1], re.MULTILINE)
    assert built() == []
    # without what the builds recorded: the cached builds record it again, before the keys of those built against them
    git(root, "checkout", "--", "xcraft.builds")
    assert xcraft("build", "--dry-run") == (0, "0 to build, 2 cached\n")
    assert built() == []


def should_count_builds_with_the_inputs_they_had(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed")
    spec = root / "base/trivial/trivial.spec"
    before = spec.read_text()
    launch = builder._Run._launch

    def edited(run: builder._Run, name: str) -> builder.Result | builder._Started:
        if name == "with-deps":  # a dependency of the build edited during the build
            spec.write_text(f"{before}# edited meanwhile\n")
        return launch(run, name)

    monkeypatch.setattr(builder._Run, "_launch", edited)
    assert built() == [
        "trivial-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm",
        "with-deps-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm",
    ]
    monkeypatch.setattr(builder._Run, "_launch", launch)
    # the counters of the inputs the builds had
    spec.write_text(before)
    assert built() == []


def should_cache_builds_under_the_suffix_of_their_rpms(root: Path) -> None:
    # a dynamic BuildRequires the build environment has already, which the lock of the main branch misses: its build
    # records it, which changes its key, and its number
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("%prep", "%generate_buildrequires\necho coreutils\n\n%prep"))
    commit_base(root)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    # cached under the release suffix of its RPMs, after its key
    (rpm,) = root.glob(".xcraft/cache/builds/*/*/trivial/*/RPMS/*/*.rpm")
    trivial = buildid.read(Project.find())["trivial"]
    assert trivial.build == 2
    assert (rpm.name, rpm.parents[2].name[64:]) == ("trivial-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm", "+b2~1.dev")


def should_refuse_numbers_given_again_on_the_main_branch(root: Path) -> None:
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    edit(root, "feature")
    assert update() == {"trivial": 2, "with-deps": 2}
    git(root, "commit", "-qam", "feature")
    git(root, "checkout", "-q", "master")
    edit(root, "master")
    assert update() == {"trivial": 2, "with-deps": 2}
    git(root, "commit", "-qam", "master")
    locked()
    # the branch merged, its conflict resolved by keeping its side: the numbers of the previous commit, for other keys
    with pytest.raises(subprocess.CalledProcessError):
        git(root, "merge", "-q", "feature")
    git(root, "checkout", "-q", "--theirs", "--", ".")
    git(root, "commit", "-qam", "merged")
    with pytest.raises(
        buildid.BuildIdError, match=r"given again to other builds, since the previous commit: trivial, with-deps$"
    ):
        locked()


def should_number_from_1_again_for_a_new_release(root: Path) -> None:
    commit_base(root)
    assert buildid.read(Project.find())["trivial"].evr == "1.0-1"
    # without the epoch-version-releases, like given before they were recorded: added, for the same numbers
    builds = root / "xcraft.builds"
    builds.write_text("".join(line for line in builds.read_text().splitlines(keepends=True) if ".evr = " not in line))
    git(root, "commit", "-qam", "without the EVRs")
    assert buildid.read(Project.find())["trivial"].evr is None
    assert update() == {"trivial": 1, "with-deps": 1}
    assert buildid.read(Project.find())["trivial"].evr == "1.0-1"
    git(root, "commit", "-qam", "with the EVRs")
    edit(root, "changed")
    assert update() == {"trivial": 2, "with-deps": 2}
    git(root, "commit", "-qam", "changed")

    def release(number: int) -> None:
        spec = root / "base/trivial/trivial.spec"
        spec.write_text(re.sub(r"^Release: \d+", f"Release: {number}", spec.read_text(), flags=re.MULTILINE))

    # a greater release: from 1, which the name of its RPMs tells apart, but not for with-deps, built against it
    release(2)
    assert update() == {"trivial": 1, "with-deps": 3}
    assert buildid.read(Project.find())["trivial"].evr == "1.0-2"
    git(root, "commit", "-qam", "release 2")
    project = Project.find()
    project.frozen = True
    buildid.update(project)  # on the main branch, checked against the previous commit
    # back to a lower one, whose numbers were given before, like 1.0-1+b2: refused
    release(1)
    with pytest.raises(buildid.BuildIdError, match=r"goes down, from that of master: trivial 1\.0-2 to 1\.0-1; "):
        buildid.update(Project.find())
    release(3)
    assert update()["trivial"] == 1


def should_reuse_the_main_branch_build_until_its_inputs_change(root: Path) -> None:
    code, output = xcraft("reuse", "with-deps")
    assert code == 1
    assert "reusing builds needs the main branch, master, in a git repository" in output
    assert xcraft("build")[0] == 0  # the builds of the main branch
    commit_base(root)
    base = buildid.read(Project.find())
    git(root, "checkout", "-qb", "feature")
    code, output = xcraft("reuse", "trivial")
    assert code == 0, output
    assert output.splitlines()[-1] == "trivial: has the build of master already, 1"
    edit(root, "changed")
    assert update() == {"trivial": 2, "with-deps": 2}
    # with-deps, built against trivial, reuses its build, accepting the change
    code, output = xcraft("reuse", "with-deps")
    assert code == 0, output
    assert output.splitlines()[-1] == "with-deps: reuses its build 1"
    deps = buildid.read(Project.find())["with-deps"]
    assert (deps.number, deps.key) == (1, base["with-deps"].key)
    assert deps.accepts
    assert (
        f'with-deps.build = 1\nwith-deps.key = "{deps.key}"\nwith-deps.accepts = "{deps.accepts}"\n'
        in (root / "xcraft.builds").read_text()
    )
    # kept while the key is the accepted one, committed or not: a main branch's build, not a development one
    assert update() == {"trivial": 2, "with-deps": 1}
    git(root, "commit", "-qam", "reused")
    assert update() == {"trivial": 2, "with-deps": 1}
    # not once the main branch has another build, merged
    git(root, "checkout", "-q", "master")
    builds = load_builds(Project.find())
    builds.ids["with-deps"] = BuildId(number=2, key="ef" * 32)
    save_builds(Project.find(), builds)
    git(root, "commit", "-qam", "renumbered")
    git(root, "checkout", "-q", "feature")
    git(root, "merge", "-q", "-X", "ours", "-m", "merged", "master")
    assert update() == {"trivial": 2, "with-deps": 3}
    git(root, "reset", "-q", "--hard", "HEAD~")
    assert built("with-deps") == ["trivial-1.0-1+b2~1.dev.xcpng8.99.noarch.rpm"]
    assert "with-deps-1.0-1+b1.xcpng8.99.noarch.rpm" in {
        p.name for p in root.glob(".xcraft/cache/builds/*/*/with-deps/*/RPMS/*/*.rpm")
    }
    # until it changes otherwise
    edit(root, "changed again")
    assert update() == {"trivial": 2, "with-deps": 2}
    assert not buildid.read(Project.find())["with-deps"].accepts
    # a package changed itself is not reused: its number stays, only its key follows
    code, output = xcraft("reuse", "trivial")
    assert code == 0, output
    assert (
        "warning: trivial: its directory changed since master, whose build cannot be reused; its builds keep the "
        "number +b2, only their key follows what they are built against"
    ) in " ".join(output.split())


def should_reuse_the_builds_built_against_a_changed_package_of_ours(root: Path) -> None:
    assert xcraft("build")[0] == 0  # the builds of the main branch
    commit_base(root)
    base = buildid.read(Project.find())
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed")  # trivial, which with-deps is built against, like xen and xapi
    assert update() == {"trivial": 2, "with-deps": 2}
    code, output = xcraft("reuse", "--built-against", "trivial")
    assert code == 0, output
    assert output.splitlines()[-1] == "with-deps: reuses its build 1"
    deps = buildid.read(Project.find())["with-deps"]
    assert (deps.number, deps.key) == (1, base["with-deps"].key)
    assert deps.accepts
    # trivial itself is built again
    assert update() == {"trivial": 2, "with-deps": 1}
    # not a package changed otherwise
    edit(root, "changed too", name="with-deps")
    assert update() == {"trivial": 2, "with-deps": 2}
    code, output = xcraft("reuse", "--built-against", "trivial")
    assert code == 0, output
    assert "reuses" not in output


def should_reuse_the_main_branch_build_a_rebuild_makes_the_same(root: Path) -> None:
    assert xcraft("build")[0] == 0  # the builds of the main branch
    commit_base(root)
    base = buildid.read(Project.find())
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed")
    # with-deps, built against trivial, built again the same: it reuses its build, accepting the change
    code, output = xcraft("build")
    assert code == 0, output
    assert "✓ with-deps built the same as its build +b1, reused" in output
    assert output.splitlines()[-1] == "1 built, 1 reused"
    ids = buildid.read(Project.find())
    assert (ids["with-deps"].number, ids["with-deps"].key) == (1, base["with-deps"].key)
    assert ids["with-deps"].accepts
    # not trivial, which changed itself
    assert "trivial: not the same as its build +b1" in output
    assert (ids["trivial"].number, ids["trivial"].accepts) == (2, None)
    # nothing to build again
    assert xcraft("build") == (0, "2 cached\n")


def should_not_reuse_the_main_branch_build_with_no_reuse(root: Path) -> None:
    assert xcraft("build")[0] == 0  # the builds of the main branch
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    edit(root, "changed")
    # with-deps, made the same, keeps its own build ID: as when another architecture lacks the build of master
    code, output = xcraft("build", "--no-reuse")
    assert code == 0, output
    assert output.splitlines()[-1] == "2 built"
    ids = buildid.read(Project.find())
    assert (ids["with-deps"].number, ids["with-deps"].accepts) == (2, None)


def should_not_reuse_builds_of_packages_with_ignored_files(root: Path) -> None:
    # in the key, which hashes all the files: not reused, or only this machine would accept its key
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    (root / ".git/info/exclude").write_text("*.orig\n")
    (root / "base/with-deps/with-deps.spec.orig").write_text("left by patch")
    code, output = xcraft("reuse", "with-deps")
    assert code == 0, output
    assert "warning: with-deps: its directory changed since master" in output


def should_not_reuse_builds_of_files_changed_through_links(root: Path) -> None:
    # a file of with-deps, which is the one of trivial
    (root / "base/trivial/notes").write_text("notes")
    (root / "base/with-deps/notes").symlink_to("../trivial/notes")
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    (root / "base/trivial/notes").write_text("changed")
    code, output = xcraft("reuse", "with-deps")
    assert code == 0, output
    assert "warning: with-deps: its directory changed since master" in output


def should_reuse_builds_across_upstream_updates(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a script run by toyrun, upstream, which rpm generates a requirement on: with-deps is built against toyrun
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace('echo "hello" > hello.txt', 'printf "#!/usr/bin/toyrun\\necho hello\\n" > hello.sh')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "install -m 755 %{name}-%{version}/hello.sh %{buildroot}/usr/share/%{name}/")
        .replace("/usr/share/%{name}/hello.txt", "/usr/share/%{name}/hello.sh")
    )  # fmt: skip
    # locked before its update, published to stay so before trivial requires it
    (root / "xcraft.toml").write_text(f'{TOML}\n[publish]\nextra = ["toyrun"]\n')
    lock = load_lock(Project.find())
    lock.sources["toyrun"] = "1-1"
    save_lock(Project.find(), lock)
    assert xcraft("build", "with-deps")[0] == 0
    commit_base(root)
    git(root, "checkout", "-qb", "feature")
    assert xcraft("lock", "--update", "toyrun")[0] == 0
    # of the toys, of gpgcheck = false: their signatures are not checked
    assert "toyrun" in load_lock(Project.find()).unsigned[ARCH]
    assert update() == {"trivial": 1, "with-deps": 2}
    # not for other updates
    code, output = xcraft("reuse", "--built-against", "trivial-upstream")
    assert code == 1
    assert "not locked: trivial-upstream" in output
    code, output = xcraft("reuse", "--built-against", "glibc")
    assert code == 0, output
    assert update() == {"trivial": 1, "with-deps": 2}
    # not with other scripts of the builds, like those of a newer xcraft, which the keys cover too
    with monkeypatch.context() as patch:
        patch.setattr(buildkey, "RECIPE", "the recipe of another xcraft")
        code, output = xcraft("reuse", "--built-against", "toyrun")
        assert code == 0, output
        assert "reuses" not in output
    update()
    code, output = xcraft("reuse", "--built-against", "toyrun")
    assert code == 0, output
    assert output.splitlines()[-1] == "with-deps: reuses its build 1"
    assert update() == {"trivial": 1, "with-deps": 1}
    # and again, merged in the main branch, for the next update
    git(root, "commit", "-qam", "reused")
    git(root, "checkout", "-q", "master")
    git(root, "merge", "-q", "--ff-only", "feature")
    git(root, "checkout", "-qb", "next")
    lock = load_lock(Project.find())
    lock.sources["toyrun"] = "3-1"  # what the builds would get, which the keys only know from the lock
    for pins in lock.binaries.values():
        pins |= {
            n: p.model_copy(update={"evr": "3-1", "sha256": "3" * 64}) for n, p in pins.items() if p.source == "toyrun"
        }
    save_lock(Project.find(), lock)
    assert update() == {"trivial": 1, "with-deps": 2}
    assert buildid.reuse(Project.find(), [], ["toyrun"]) == ["with-deps"]
    assert update() == {"trivial": 1, "with-deps": 1}


def should_update_the_build_ids_without_building(root: Path) -> None:
    code, output = xcraft("build", "--ids")
    assert code == 0, output
    assert set(load_builds(Project.find()).ids) == {"trivial", "with-deps"}
    assert "build IDs: trivial 1, with-deps 1" in output
    assert xcraft("build", "--ids")[1].endswith("the build IDs are up to date\n")
    # a change, which --locked refuses, then updates the IDs of trivial and of with-deps, built against it
    key = load_builds(Project.find()).ids["trivial"].key
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello again"'))
    code, output = xcraft("build", "--ids", "--locked")
    assert code == 1
    assert "the build IDs would change, which --locked forbids: trivial, with-deps" in output
    code, output = xcraft("build", "--ids")
    assert code == 0, output
    assert "build IDs: trivial 1, with-deps 1" in output
    assert load_builds(Project.find()).ids["trivial"].key != key
    assert not (root / ".xcraft/cache/builds").exists()  # nothing built
    code, output = xcraft("build", "trivial", "--ids")
    assert code == 2
    assert "--ids updates the build IDs of all the packages" in " ".join(output.replace("│", "").split())


def should_keep_the_dune_cache_of_development_builds(root: Path) -> None:
    # the cache of dune of trivial, which writes in it like dune: kept on this machine, for the development builds
    # only, but for the builds without dune
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text().replace(
            'echo "hello" > hello.txt',
            'echo "hello" > hello.txt\n'
            'test -z "${DUNE_CACHE_ROOT:-}" || echo "$DUNE_CACHE $DUNE_CACHE_STORAGE_MODE" > "$DUNE_CACHE_ROOT/entry"',
        )
    )
    commit_base(root)
    code, output = xcraft("build")
    assert code == 0, output
    caches = root / ".xcraft/dune-cache"
    assert not caches.exists() or not any(caches.iterdir())
    git(root, "checkout", "-qb", "feature")
    spec.write_text(f"# changed\n{spec.read_text()}")
    code, output = xcraft("build")
    assert code == 0, output
    assert (caches / "trivial/entry").read_text() == "enabled copy\n"
    assert [p.name for p in caches.iterdir()] == ["trivial"]  # not with-deps, without dune
    assert xcraft("clean", "trivial")[0] == 0
    assert (caches / "trivial/entry").exists()
    assert xcraft("clean", "trivial", "--dune-cache")[0] == 0
    assert not (caches / "trivial").exists()
