"""Builds in the real build environment, on the fixture packages: their locks and pins, their recordings, the
snapshots, and the commands needing the image.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tests import ARCH, TOML, TOYS, copy_project, git, results, runner, write_spec, xcraft
from xcraft import build as builder
from xcraft import buildid, clean, container, localrepo, pinned, process, publish, resolution
from xcraft.__main__ import app
from xcraft.builds import builds_path, load_builds, save_builds
from xcraft.cache import BuildError
from xcraft.iso import _INSTALL_TOOLS, _check_installed_tools
from xcraft.localrepo import LocalRepo
from xcraft.lock import Recorded, load_lock, lock_path, save_lock
from xcraft.project import Project
from xcraft.publish import _check_installable
from xcraft.resolution import ISO_TOOLS, ResolutionError
from xcraft.sources import write_manifest


def states() -> dict[str, str]:
    """Return the state of each package shown by xcraft status."""
    return {line.split()[0]: line.split()[2] for line in xcraft("status", "--full")[1].splitlines()[:-1]}


def should_build_against_local_builds_and_cache_them(root: Path) -> None:
    assert states() == {"trivial": "missing", "with-deps": "missing"}
    # with-deps BuildRequires trivial, which only exists once built locally
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == [
        "✓ trivial built",
        "✓ with-deps built",
        "2 built",
    ]
    code, output = xcraft("build", "with-deps")
    assert code == 0
    assert output.splitlines() == ["2 cached"]  # nothing to do: no package shown
    assert xcraft("build", "with-deps", "--dry-run") == (0, "0 to build, 2 cached\n")
    assert list(root.glob(f".xcraft/cache/builds/*/{ARCH}/with-deps/*/RPMS/noarch/with-deps-1.0-*.noarch.rpm"))

    # a change in trivial changes the key of with-deps too
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello again"'))
    assert states() == {"trivial": "outdated", "with-deps": "outdated"}
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert output.splitlines()[-1] == "2 built"
    assert states() == {"trivial": "ready", "with-deps": "ready"}


def _timed(root: Path, name: str, requires: str = "", build: str = "") -> None:
    """Write a package whose build writes when it starts and ends in its log, after `build`."""
    write_spec(root, name, requires, files=f"/usr/share/xcraft-test/{name}")
    spec = root / f"base/{name}/{name}.spec"
    timed = f"%build\n{build}\necho start=$(date +%s.%N)\nsleep 2\necho end=$(date +%s.%N)\n%install\n"
    spec.write_text(spec.read_text().replace("%install\n", timed))


def _span(root: Path, name: str) -> tuple[float, float]:
    """Return when the build of `name` started and ended, as its log says, see _timed()."""
    log = root.joinpath(f".xcraft/logs/{name}.log").read_text()
    return float(re.findall(r"^start=(\S+)$", log, re.MULTILINE)[-1]), float(
        re.findall(r"^end=(\S+)$", log, re.MULTILINE)[-1]
    )


def should_build_the_packages_independent_of_each_other_at_once(root: Path) -> None:
    _timed(root, "first")
    _timed(root, "second")
    _timed(root, "after-first", requires="first")
    code, output = xcraft("build", "--jobs", "3", "first", "second", "after-first")
    assert code == 0, output
    assert results(output)[-1] == "3 built"
    first, second, after = (_span(root, name) for name in ("first", "second", "after-first"))
    # at the same time
    assert first[0] < second[1]
    assert second[0] < first[1]
    assert after[0] > first[1]  # once what it is built against is built


def should_show_the_warnings_of_builds_running_at_once_after_their_names(root: Path) -> None:
    _timed(root, "first", build="echo 'warning: from first'")
    _timed(root, "second", build="echo 'warning: from second'")
    result = runner.invoke(app, ["build", "--jobs", "2", "first", "second"])
    assert result.exit_code == 0, result.output
    assert "first  | warning: from first" in result.stderr
    assert "second | warning: from second" in result.stderr


def should_build_all_the_packages_when_none_is_given(root: Path) -> None:
    code, output = xcraft("build", "--dry-run")
    assert code == 0, output
    assert output.splitlines() == ["build    trivial", "build    with-deps", "2 to build"]


def should_show_the_diagnostics_after_the_names_of_their_packages_in_ci(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CI", "true")
    result = runner.invoke(app, ["build", "--jobs", "1"])
    assert result.exit_code == 0, result.output
    # padded to the longest name, with-deps
    assert "trivial   | warning: bogus date in %changelog" in result.stderr
    assert "building trivial" not in result.stderr  # nor a header of its step


def should_not_cache_a_build_when_its_spec_changed_meanwhile(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = root / "base/trivial/trivial.spec"
    run_build = builder.run_build

    def changed(*args: object, **kwargs: object) -> None:
        run_build(*args, **kwargs)  # ty: ignore[invalid-argument-type]
        spec.write_text(f"{spec.read_text()}# changed\n")  # during the build, once staged

    monkeypatch.setattr(builder, "run_build", changed)
    code, output = xcraft("build", "trivial")
    assert code == 1
    assert "base/trivial changed during the build, build it again" in output
    assert states() == {"trivial": "missing", "with-deps": "missing"}


def should_not_cache_a_build_when_its_spec_changed_since_the_builds_started(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = root / "base/trivial/trivial.spec"
    resolved_root = builder.resolved_root

    def changed(project: Project, name: str) -> object:
        # after the check of the directories of the keys, before the build tree has its files
        spec.write_text(f"{spec.read_text()}# changed\n")
        return resolved_root(project, name)

    monkeypatch.setattr(builder, "resolved_root", changed)
    code, output = xcraft("build", "trivial")
    assert code == 1
    assert "base/trivial changed since the builds started, build it again" in output
    assert not list(root.glob(".xcraft/cache/builds/*/*/trivial"))


HOST_ENTRIES = "" if ARCH == "x86_64_v2" else f"{ARCH}."
"""The prefix of the entries of the lock the builds on the host record, not shared but on the first architecture."""
UP_TO_DATE = (
    "versions up to date, not looking for other contents of the same versions\n"
    "hint: xcraft lock --update looks for them too"
)


def should_refuse_a_binary_package_made_by_two_packages(root: Path) -> None:
    write_spec(root, "twin")
    spec = root / "base/twin/twin.spec"
    spec.write_text(spec.read_text().replace("Name: twin", "Name: trivial"))
    code, output = xcraft("build", "--dry-run")
    assert code == 1
    # on the architecture parsed first, maybe not the host's
    assert re.search(r"binary packages of several packages for \S+: trivial \(trivial, twin\)", output)


def should_build_without_network_privileges_or_broken_pipes(root: Path) -> None:
    # without network but its loopback interface, without privileges, and with the pipes closed by their reader killing
    # their writer, as outside of rpmbuild
    write_spec(root, "sandboxed")
    spec = root / "base/sandboxed/sandboxed.spec"
    network = (
        "import socket\n"
        "server = socket.create_server(('127.0.0.1', 0))\n"
        "socket.create_connection(server.getsockname())\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 443), timeout=5)\n"
        "except OSError:\n"
        "    print('no network')\n"
        "else:\n"
        "    raise SystemExit('network')\n"
    )
    checks = (
        f"python3 - <<'EOF'\n{network}EOF\n"
        "if sudo true; then exit 1; fi\n"
        "seq 1000000 2>seq.err | head -1\ntest ! -s seq.err || { cat seq.err; exit 1; }\n"
    )
    spec.write_text(spec.read_text().replace("%install\n", f"%build\n{checks}%install\n"))
    code, output = xcraft("build", "sandboxed")
    assert code == 0, output
    log = (root / ".xcraft/logs/sandboxed.log").read_text()
    assert "no network" in log
    assert '"no new privileges" flag is set' in log


def should_build_again_after_dynamic_buildrequires_on_ours(root: Path) -> None:
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(spec.read_text().replace("BuildRequires: trivial\n", "").replace(
        "%prep", "%generate_buildrequires\necho trivial\n\n%prep"
    ))  # fmt: skip
    write_spec(root, "user", "with-deps")
    # not known to need trivial: built alone, then again after it, and what needs it skipped meanwhile
    code, output = xcraft("build", "user")
    assert code == 0, output
    assert "↻ with-deps: " in output
    assert "- user skipped, needs with-deps" in output
    assert [line.split(" built")[0] for line in results(output) if line[0] == "✓"] == [
        "✓ trivial",
        "✓ with-deps",
        "✓ user",
    ]
    assert load_builds(Project.find()).buildrequires.of(ARCH) == {"with-deps": ["trivial"]}


def should_record_the_files_our_builds_provide(root: Path) -> None:
    # xcraft does not know which files our packages provide before they are built
    write_spec(root, "tool", files="/usr/bin/xcraft-test-tool")
    write_spec(root, "tool-user", "/usr/bin/xcraft-test-tool")
    code, output = xcraft("build", "tool-user")
    assert code == 1
    assert "its build root cannot be resolved: nothing provides /usr/bin/xcraft-test-tool" in output
    # then the build of tool records it
    code, output = xcraft("build", "tool")
    assert code == 0, output
    code, output = xcraft("build", "tool-user")
    assert code == 0, output
    assert load_builds(Project.find()).provides.of(ARCH) == {"tool": ["/usr/bin/xcraft-test-tool"]}


def should_refuse_build_roots_with_upstream_packages_named_like_ours(root: Path) -> None:
    # upstream's bc, instead of ours, never built since nothing is known to need it
    write_spec(root, "bc")
    write_spec(root, "bc-user", "/usr/bin/bc")
    code, output = xcraft("build", "bc-user")
    assert code == 1
    assert "its build root has the upstream bc (ours from bc)" in output


def should_put_ours_in_build_roots_instead_of_upstream_ones(root: Path) -> None:
    # kernel-headers comes with gcc, through glibc-devel: ours rather than upstream's, built first
    write_spec(root, "kernel-headers", files="/usr/include/xcraft-test.h")
    spec = root / "base/kernel-headers/kernel-headers.spec"
    spec.write_text(spec.read_text().replace("Version: 1.0", "Version: 99"))
    write_spec(root, "compiled", "gcc")
    code, output = xcraft("build", "compiled")
    assert code == 0, output
    assert [line.split(" built")[0] for line in results(output)[:2]] == ["✓ kernel-headers", "✓ compiled"]
    project = Project.find()
    assert "kernel-headers" in project.deps["compiled"]
    # the upstream one in the lock, like for the image, which the build does not get
    roots = load_lock(project).roots
    assert "kernel-headers" in roots.of(ARCH)["compiled"]
    assert re.search(
        r"^ kernel-headers .* xcraft-[0-9a-f]{8}-compiled\s", project.log("compiled").read_text(), re.MULTILINE
    )
    # and ccache, for a compiler
    assert "ccache" in roots.shared["compiled"]


def should_replace_image_packages_by_ours_in_build_roots(root: Path) -> None:
    # openblas-srpm-macros is in the image, and harmless to replace, by one requiring a pinned package it does not have
    write_spec(root, "openblas-srpm-macros", files="/usr/share/xcraft-test/openblas-srpm-macros")
    spec = root / "base/openblas-srpm-macros/openblas-srpm-macros.spec"
    spec.write_text(spec.read_text().replace("Summary: test", "Requires: toyrun\nSummary: test"))
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert [line.split(" built")[0] for line in results(output)[:2]] == ["✓ openblas-srpm-macros", "✓ trivial"]
    log = root.joinpath(".xcraft/logs/trivial.log").read_text()
    assert re.search(r"^Downgrading:\n openblas-srpm-macros .* xcraft-[0-9a-f]{8}-trivial\s", log, re.MULTILINE)
    assert re.search(r"^ toyrun .* xcraft-pinned ", log, re.MULTILINE), log


def should_share_the_dnf_lock_between_containers(root: Path) -> None:
    logs = [root / f"lock-{i}.log" for i in range(2)]
    container.built_image(Project.find())  # before the threads, which would both build it

    def hold(log: Path) -> None:
        script = f"{container.DNF_LOCK} sh -c 'date +%s.%N; sleep 1; date +%s.%N'"
        container.run_container(Project.find(), script, container=f"xcraft-test-{log.stem}", volumes=[], log=log)

    threads = [threading.Thread(target=hold, args=(log,)) for log in logs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    (a_start, a_end), (b_start, b_end) = (sorted(map(float, log.read_text().split())) for log in logs)
    assert a_end <= b_start or b_end <= a_start


def should_cache_a_build_back_at_the_key_of_a_former_one(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    # dynamic BuildRequires its build does not find: missing, and built again, back to the key of the first build
    builds = load_builds(Project.find())
    builds.buildrequires.shared["trivial"] = ["zlib-ng-compat-devel"]
    save_builds(Project.find(), builds)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == ["✓ trivial built", "1 built"]
    assert states() == {"trivial": "ready", "with-deps": "missing"}
    assert not list(root.glob(f".xcraft/cache/builds/*/{ARCH}/trivial/.*"))


def should_skip_dependents_when_a_build_fails(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "build failure"; exit 3'))
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    code, output = xcraft("build", "with-deps")
    assert code == 1
    assert "✗ trivial: failed, see " in output
    # the whole log, folded, in CI, then the failed rpmbuild step
    log = output[output.index("::group::full trivial build log\n") : output.index("::endgroup::\n")]
    assert "Executing(%prep)" in log
    assert "echo 'build failure'" in output[output.index("::endgroup::\n") :]
    assert "- with-deps skipped, needs trivial" in output
    assert output.splitlines()[-1] == "1 failed, 1 skipped"
    code, output = xcraft("log", "trivial", "--step", "build", "--grep", "failure")
    assert code == 0
    assert "build failure" in output


def should_report_missing_archives_and_broken_specs(root: Path) -> None:
    write_manifest(root / "base/trivial/sources", {"trivial-1.0.tar.gz": "0" * 64})
    (root / "base/broken").mkdir()
    (root / "base/broken/broken.spec").write_text("Name: broken\n%if\n")
    code, output = xcraft("build", "trivial", "broken")
    assert code == 1
    assert "trivial-1.0.tar.gz is not in the source cache, the remote storage, nor at a URL of the spec" in output
    assert "✗ broken: " in output
    assert re.search(r"^broken +error *$", xcraft("status")[1], re.MULTILINE)


@pytest.fixture
def me(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make git give the changelog entries a user of its global configuration."""
    (gitconfig := tmp_path / "gitconfig").write_text("[user]\n\tname = Me\n\temail = me@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))


@pytest.mark.usefixtures("me")
def should_bump_the_release_with_a_changelog_entry(root: Path) -> None:
    code, output = xcraft("bump", "trivial", "-m", "Rebuild")
    assert code == 0, output
    assert output == "trivial: 1.0-1 -> 1.0-2\n"
    spec = (root / "base/trivial/trivial.spec").read_text()
    assert "Release: 2%{?dist}\n" in spec
    assert "%changelog\n* " in spec
    assert " Me <me@example.com> - 1.0-2\n- Rebuild\n\n* Mon Jul 10 2026 Test User" in spec
    # without a message, an empty item to fill in, which rpm accepts
    code, output = xcraft("bump", "trivial")
    assert code == 0, output
    assert " Me <me@example.com> - 1.0-3\n-\n\n" in (root / "base/trivial/trivial.spec").read_text()
    assert xcraft("build", "trivial")[0] == 0


@pytest.mark.usefixtures("me")
def should_bump_the_version_of_packages_sharing_an_archive(root: Path) -> None:
    archives = root / "archives"
    archives.mkdir()
    (archives / "trivial-2.0.tar.gz").write_bytes(b"new")
    spec = root / "base/trivial/trivial.spec"
    source = f"Source0: file://{archives}/trivial-%{{version}}.tar.gz\n"
    spec.write_text(spec.read_text().replace("License: MIT\n", f"License: MIT\n{source}"))
    (root / "base/trivial/sources").write_text(f"{'0' * 64}  trivial-1.0.tar.gz\n")
    # another package of the same archive, through a link
    twin = root / "base/twin"
    twin.mkdir()
    (twin / "twin.spec").write_text(spec.read_text().replace("Name: trivial", "Name: twin"))
    (twin / "sources").symlink_to("../trivial/sources")
    (root / "xcraft.toml").write_text(f'same_version = [["trivial", "twin"]]\n{TOML}')
    code, output = xcraft("bump", "twin", "--version", "2.0")
    assert code == 0, output
    assert "trivial: 1.0-1 -> 2.0-1\ntwin: 1.0-1 -> 2.0-1\n" in output
    assert "Version: 2.0\nRelease: 1%{?dist}\n" in spec.read_text()
    assert " Me <me@example.com> - 2.0-1\n- Update to 2.0\n" in (twin / "twin.spec").read_text()
    digest = hashlib.sha256(b"new").hexdigest()
    assert (root / "base/trivial/sources").read_text() == f"{digest}  trivial-2.0.tar.gz\n"
    assert (twin / "sources").is_symlink()
    assert (root / ".xcraft/cache/sources" / digest / "trivial-2.0.tar.gz").read_bytes() == b"new"


@pytest.mark.usefixtures("me")
@pytest.mark.parametrize("version", ["1.9", "1.10", "1.010", "1.10~rc1"])
def should_refuse_a_bump_to_a_version_not_greater(root: Path, version: str) -> None:
    twin = root / "base/twin"
    twin.mkdir()
    spec = root / "base/trivial/trivial.spec"
    (twin / "twin.spec").write_text(
        spec.read_text()
        .replace("Name: trivial", "Name: twin")
        .replace("Version: 1.0", "%global upver 1.10\nVersion: %{upver}")
    )
    (root / "xcraft.toml").write_text(f'same_version = [["trivial", "twin"]]\n{TOML}')
    paths = [spec, twin / "twin.spec", root / "base/trivial/sources", root / "xcraft.lock"]
    before = {path: path.read_bytes() for path in paths}
    code, output = xcraft("bump", "trivial", "--version", version)
    assert code == 1
    assert f"twin: --version {version} must be greater than current version 1.10" in output
    assert {path: path.read_bytes() for path in paths} == before


def should_refuse_bumps_and_patchsets_of_no_known_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    copy_project(tmp_path / "project")
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path / "project"))
    for args, error in [(["bump"], "no package given"),
                        (["patchset", str(tmp_path), "--package", "nope"], "unknown package: nope")]:  # fmt: skip
        code, output = xcraft(*args)
        assert code == 2
        assert error in output


@pytest.mark.parametrize("output", ["yes looping", "yes looping | tr -d '\\n'"])  # in lines, or in a single one
def should_stop_a_build_when_its_log_grows_too_large(output: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "MAX_LOG_SIZE", 2**20)
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello" > hello.txt', output))
    code, output = xcraft("build", "trivial")
    assert code == 1
    assert "✗ trivial: stopped, its log got larger than 1 MiB" in output
    name = f"xcraft-{Project.find().id}-trivial"
    assert subprocess.run(["podman", "container", "exists", name], check=False).returncode != 0


def should_lock_upstream_sources_when_builds_need_them(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    lock = load_lock(Project.find())
    assert "rpm" in lock.sources  # of the image
    builds = load_builds(Project.find())
    assert (builds.requires, builds.provides, builds.buildrequires) == (Recorded(), Recorded(), Recorded())

    # a BuildRequires of a source package not locked yet
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nBuildRequires: lzo-devel"))
    code, output = xcraft("build", "trivial", "--locked")
    assert code == 1
    assert re.search(r"upstream packages not locked, which --locked forbids: .*\blzo\b", output), output
    assert "lzo" not in load_lock(Project.find()).sources
    assert xcraft("build", "trivial")[0] == 0
    assert "lzo" in load_lock(Project.find()).sources
    assert xcraft("build", "trivial", "--locked") == (0, "1 cached\n")
    assert xcraft("lock", "--update", "lzo-devel")[1] == "0 updated\n"


def should_refuse_to_pin_upstream_packages_of_several_architectures(root: Path) -> None:
    # toymulti, for x86_64 and i686 in the repositories of x86_64: one architecture would be pinned by chance
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nBuildRequires: toymulti"))
    code, output = xcraft("build", "trivial", "--dry-run")
    assert code == 1
    assert "upstream packages of several architectures, which the lock cannot pin: toymulti" in output, output


def should_lock_versions_that_all_the_architectures_have(root: Path) -> None:
    # toyarch 2 is only for x86_64: toyarch is locked in the version all the architectures have
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nBuildRequires: toyarch"))
    # toyrun, published, locked in an older version, has a newer one all the architectures have
    (root / "xcraft.toml").write_text(f'{TOML}\n[publish]\nextra = ["toyrun"]\n')
    lock = load_lock(Project.find())
    lock.sources["toyrun"] = "1-1"
    save_lock(Project.find(), lock)
    assert xcraft("build", "trivial")[0] == 0
    assert load_lock(Project.find()).sources["toyarch"] == "1-1"
    assert xcraft("lock", "--outdated", "toyarch") == (0, f"{UP_TO_DATE}\n")
    # by binary name, of the locked versions only
    assert xcraft("lock", "--outdated", "toyarch-new")[1].endswith("not locked: toyarch-new")
    expected = "toyrun 1-1 -> 2-1\n1 to update\nhint: xcraft lock --update updates them\n"
    assert xcraft("lock", "--outdated", "toyarch", "toyrun") == (1, expected)
    assert load_lock(Project.find()).sources["toyrun"] == "1-1"
    assert xcraft("lock", "--update", "toyarch", "toyrun") == (0, "toyrun 1-1 -> 2-1\n1 updated\n")
    assert load_lock(Project.find()).sources["toyrun"] == "2-1"


def should_pin_what_is_published_on_all_the_architectures(root: Path) -> None:
    (root / "xcraft.toml").write_text(f'{TOML}\n[publish]\nextra = ["bc"]\n')
    assert xcraft("build", "--dry-run")[0] == 0
    lock = load_lock(Project.find())
    # what is published, pinned when preparing, for all the architectures
    assert lock.runtime.keys() == {"x86_64_v2", "aarch64"}
    assert {"bc", "glibc"} <= set(lock.runtime[ARCH]) <= lock.binaries[ARCH].keys()
    # what the lock has, until it can be resolved, which publishing needs
    (root / "xcraft.toml").write_text(f'{TOML}\n[publish]\nextra = ["no-such-package"]\n')
    assert xcraft("build", "--dry-run")[0] == 0
    assert load_lock(Project.find()).runtime == lock.runtime
    with pytest.raises(ResolutionError, match=f"cannot install, on {ARCH}: no-such-package"):
        resolution.published(Project.find())
    # the sources of what is published no more leave the lock, the others keep their versions
    (root / "xcraft.toml").write_text(TOML)
    assert xcraft("build", "--dry-run")[0] == 0
    sources = load_lock(Project.find()).sources
    assert "bc" in lock.sources
    assert "bc" not in sources
    assert sources == {s: v for s, v in lock.sources.items() if s in sources}


@pytest.mark.skipif(ARCH != "x86_64_v2", reason="the tools of the ISO are for x86_64")
def should_pin_and_install_the_tools_making_the_iso(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # with bc, which nothing else needs, in the repository of the ISO: published, for the snapshot to have it
    iso = 'installer = ["bash"]\npackages = ["bc"]\n'
    (root / "xcraft.toml").write_text(f"{TOML}{iso}")
    assert xcraft("build", "--dry-run")[0] == 0
    project = Project.find()
    lock = load_lock(project)
    # pinned when preparing, with what the installer needs
    assert set(ISO_TOOLS) <= set(lock.iso[project.arch]) <= lock.binaries[project.arch].keys()
    assert {"bash", "glibc", "bc"} <= set(lock.runtime[project.arch]) <= lock.binaries[project.arch].keys()
    assert "bc" not in lock.runtime["aarch64"]
    assert {"dosfstools", "libisoburn", "memtest86+", "mtools", "syslinux"} <= lock.sources.keys()  # by source
    assert lock.iso.keys() == {project.arch}
    # the tools, which cannot be installed, kept as the lock has them, but not what is published
    with monkeypatch.context() as patch:
        patch.setattr(resolution, "ISO_TOOLS", [*ISO_TOOLS, "no-such-package"])
        (root / "xcraft.toml").write_text(f'{TOML}{iso}\n[publish]\nextra = ["toyrun"]\n')
        assert xcraft("build", "--dry-run")[0] == 0
    updated = load_lock(project)
    assert "toyrun" in updated.runtime[ARCH]
    assert updated.iso == lock.iso
    lock = updated
    # installed from the pinned packages
    with project.tmp_dir("tools") as work:
        record = f"{container.DNF_LOCK} python3 /xcraft/record.py /work/tools.json"
        container.run(project, "tools", f"{_INSTALL_TOOLS} && {record}", volumes=[f"{work}:/work"], install=True)
        _check_installed_tools(project, work / "tools.json")
        del lock.iso[project.arch][0]
        save_lock(project, lock)
        with pytest.raises(RuntimeError, match="the tools making the ISO are not those pinned in the lock: "):
            _check_installed_tools(project, work / "tools.json")


@pytest.mark.parametrize(("kind", "package"), [("sign", "rpm-sign"), ("lint", "rpmlint")])
def should_pin_the_side_images_apart_from_the_build_image(root: Path, kind: str, package: str) -> None:
    project = Project.find()
    lock = load_lock(project)
    # pinned when preparing, on every architecture, without the image's
    assert getattr(lock, kind).keys() == lock.image.keys()
    for arch, names in getattr(lock, kind).items():
        assert {package} <= set(names) <= lock.binaries[arch].keys()
        assert not set(names) & set(lock.image[arch])
    # rpm-sign of the locked rpm
    assert lock.binaries[ARCH]["rpm-sign"].evr == lock.binaries[ARCH]["rpm"].evr
    # installed from the pinned packages
    image = container.side_image(project, kind)
    installed = container.image_packages(project, image)
    assert all(installed[n].evr == lock.binaries[ARCH][n].evr for n in getattr(lock, kind)[ARCH])


def should_publish_image_packages_that_our_packages_require(root: Path) -> None:
    # a script of trivial run by sh, of bash, in the image: published, as the snapshot does not have the image
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace('echo "hello" > hello.txt', 'printf "#!/bin/sh\\necho hello\\n" > hello.sh')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "install -m 755 %{name}-%{version}/hello.sh %{buildroot}/usr/share/%{name}/")
        .replace("/usr/share/%{name}/hello.txt", "/usr/share/%{name}/hello.sh")
    )  # fmt: skip
    assert xcraft("build", "--dry-run")[0] == 0
    assert "bash" not in load_lock(Project.find()).runtime.get(ARCH, [])
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert load_builds(Project.find()).requires.of(ARCH)["trivial"] == ["bash"]
    assert xcraft("build", "--dry-run")[0] == 0
    assert "bash" in load_lock(Project.find()).runtime[ARCH]


def should_refuse_a_snapshot_when_the_checkout_changed_meanwhile(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # the lock of another git checkout, once the builds are done: the snapshot is of the one before, not of its keys
    checked = publish._check_installable

    def changed(project: Project, repos: list[Path]) -> None:
        checked(project, repos)
        (lock := lock_path(project)).write_text(f"{lock.read_text()}# of another checkout\n")

    monkeypatch.setattr(publish, "_check_installable", changed)
    with pytest.raises(BuildError, match="the checkout changed while making the snapshot"):
        publish.snapshot([Project.find()], tmp_path / "snapshot", signer=None, storage=None, build=False)


def should_check_each_snapshot_once_with_its_own_metadata(root: Path, tmp_path: Path) -> None:
    # like those of two projects: the broken one, made first, is not checked with the newer metadata of the other, which
    # the dnf cache shared by the containers keeps
    project = Project.find()
    snapshots = {}
    for snapshot, base, build in (("broken", "toyrun-1", ["toyrun-bin-2"]), ("other", "toyrun-2", [])):
        snapshots[snapshot] = [LocalRepo(project, tmp_path / snapshot / repo) for repo in ("base", "build")]
        for repo, names in zip(snapshots[snapshot], ([base], build), strict=True):
            repo.reset(extra_upstream=False)
            for name in names:
                localrepo.link(TOYS / ARCH / f"{name}-1.noarch.rpm", repo.path / f"{name}-1.noarch.rpm")
            repo.index()
    _check_installable(project, [repo.path for repo in snapshots["other"]])
    checked = project.log("_snapshot").stat().st_mtime_ns
    _check_installable(project, [repo.path for repo in snapshots["other"]])
    assert project.log("_snapshot").stat().st_mtime_ns == checked
    with pytest.raises(BuildError, match="not self-contained"):  # toyrun-bin 2 needs toyrun 2
        _check_installable(project, [repo.path for repo in snapshots["broken"]])


def run_by_xtool(root: Path) -> None:
    """Make trivial install a script run by xtool, of ours too, built after it."""
    (root / "base/xtool").mkdir()
    (root / "base/xtool/xtool.spec").write_text(
        "Name: xtool\nVersion: 1.0\nRelease: 1%{?dist}\nSummary: An interpreter\nLicense: MIT\nBuildArch: noarch\n"
        "%description\nAn interpreter.\n%install\nmkdir -p %{buildroot}/usr/bin\n"
        "printf '#!/bin/sh\\nexec sh \"$@\"\\n' > %{buildroot}/usr/bin/xtool\nchmod 755 %{buildroot}/usr/bin/xtool\n"
        "%files\n/usr/bin/xtool\n"
    )
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace('echo "hello" > hello.txt', 'printf "#!/usr/bin/xtool\\necho hello\\n" > hello.sh')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "install -m 755 %{name}-%{version}/hello.sh %{buildroot}/usr/share/%{name}/")
        .replace("/usr/share/%{name}/hello.txt", "/usr/share/%{name}/hello.sh")
    )  # fmt: skip


def should_record_requirements_once_what_they_need_is_built(root: Path) -> None:
    # built together: recorded once xtool is built, without warning
    run_by_xtool(root)
    code, output = xcraft("build", "trivial", "xtool")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == ["✓ trivial built", "✓ xtool built", "2 built"]
    assert "cannot record" not in output
    assert load_builds(Project.find()).requires.of(ARCH)["trivial"] == ["xtool"]
    assert xcraft("build", "trivial", "xtool", "--locked") == (0, "2 cached\n")


def should_record_the_requirements_of_a_cached_build_once_what_they_need_is_built(root: Path) -> None:
    # built alone first, with a warning, then cached
    run_by_xtool(root)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert "cannot record what rpm generated for trivial" in output
    code, output = xcraft("build", "trivial", "xtool")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == ["✓ xtool built", "1 built, 1 cached"]
    assert "cannot record" not in output
    assert load_builds(Project.find()).requires.of(ARCH)["trivial"] == ["xtool"]


def should_defer_recording_until_what_is_required_is_built(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    # trivial requires what xtool, of ours, provides: recorded once xtool is built, without the problem of dnf
    (root / "base/xtool").mkdir()
    (root / "base/xtool/xtool.spec").write_text(
        "Name: xtool\nVersion: 1.0\nRelease: 1%{?dist}\nSummary: A tool\nLicense: MIT\nBuildArch: noarch\n"
        "Provides: xtool-core\n%description\nA tool.\n%install\nmkdir -p %{buildroot}/usr/share/xtool\n"
        "touch %{buildroot}/usr/share/xtool/core\n%files\n/usr/share/xtool\n"
    )
    # and another package, unrelated
    (root / "base/other").mkdir()
    (other := root / "base/other/other.spec").write_text(
        "Name: other\nVersion: 1.0\nRelease: 1%{?dist}\nSummary: Another\nLicense: MIT\nBuildArch: noarch\n"
        "%description\nAnother.\n%install\nmkdir -p %{buildroot}/usr/share/other\n%files\n/usr/share/other\n"
    )
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nRequires: xtool-core"))
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        code, output = xcraft("build", "trivial", "other")
    assert code == 0, output
    # only in the debug log: recorded once xtool is built, by whatever builds it, with nothing to do
    assert "the dependencies rpm generated for trivial are recorded once xtool, which it needs, is built" in caplog.text
    assert "recorded once" not in output
    assert "cannot record" not in output
    # not resolved again by the new builds of other packages, while xtool has no build
    (recorded,) = (root / ".xcraft/cache/builds").glob("*/*/trivial/*/rpm-deps.json")
    resolved = recorded.stat().st_mtime_ns
    other.write_text(f"# changed\n{other.read_text()}")
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        code, output = xcraft("build", "other")
    assert code == 0, output
    assert "recorded once xtool" in caplog.text
    assert recorded.stat().st_mtime_ns == resolved
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        code, output = xcraft("build", "trivial", "xtool")
    assert code == 0, output
    assert "recorded once" not in caplog.text
    assert load_builds(Project.find()).requires.of(ARCH).get("trivial") is None  # what it requires, declared


def should_record_requirements_as_soon_as_they_resolve(root: Path, caplog: pytest.LogCaptureFixture) -> None:
    # trivial, of dynamic BuildRequires, whose script requires tcl, recorded at once, while trivial-extra, of the same
    # build, requires what xtool, of ours, provides, recorded once xtool is built: the builds against trivial get tcl
    # before
    (root / "base/xtool").mkdir()
    (root / "base/xtool/xtool.spec").write_text(
        "Name: xtool\nVersion: 1.0\nRelease: 1%{?dist}\nSummary: A tool\nLicense: MIT\nBuildArch: noarch\n"
        "Provides: xtool-core\n%description\nA tool.\n%install\nmkdir -p %{buildroot}/usr/share/xtool\n"
        "touch %{buildroot}/usr/share/xtool/core\n%files\n/usr/share/xtool\n"
    )
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace("%prep", "%generate_buildrequires\necho zlib-ng-compat-devel\n\n%prep")
        .replace('echo "hello" > hello.txt', 'printf "#!/usr/bin/tclsh\\nputs hello\\n" > hello.tcl')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "install -m 755 %{name}-%{version}/hello.tcl %{buildroot}/usr/share/%{name}/\n"
                 "touch %{buildroot}/usr/share/%{name}/extra")
        .replace("%files\n/usr/share/%{name}/hello.txt",
                 "%package extra\nSummary: Extra\nRequires: xtool-core\n%description extra\nExtra.\n"
                 "%files extra\n/usr/share/%{name}/extra\n\n%files\n/usr/share/%{name}/hello.tcl")
    )  # fmt: skip
    buildid.update(Project.find())  # the build IDs of the changed spec, --locked checks before building
    code, output = xcraft("build", "trivial", "--locked")
    assert code == 1
    # those of the architecture of the host, which are shared once found on the first one
    assert f'+trivial.{HOST_ENTRIES}buildrequires = ["zlib-ng-compat-devel"]' in output
    with caplog.at_level(logging.DEBUG, logger="xcraft"):
        code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert load_builds(Project.find()).buildrequires.of(ARCH) == {"trivial": ["zlib-ng-compat-devel"]}
    assert (
        "the dependencies rpm generated for trivial-extra are recorded once xtool, which it needs, is built"
        in caplog.text
    )
    assert load_builds(Project.find()).requires.of(ARCH) == {"trivial": ["tcl"]}
    assert "tcl" in resolution.resolve(Project.find()).buildroots["with-deps"]
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert xcraft("build", "with-deps", "--locked")[0] == 0


def should_not_treat_a_digit_in_a_name_as_its_release(root: Path) -> None:
    # the digit of its release in its name, without %dist: not named after its release
    write_spec(root, "libfoo1")
    spec = root / "base/libfoo1/libfoo1.spec"
    spec.write_text(spec.read_text().replace("Release: 1%{?dist}", "Release: 1%{?autorev}"))
    code, output = xcraft("build", "libfoo1")
    assert code == 0, output
    assert [p.name for p in root.glob(".xcraft/cache/builds/*/*/libfoo1/*/RPMS/*/*.rpm")] == [
        "libfoo1-1.0-1+b1.noarch.rpm"
    ]


def should_record_binaries_named_with_their_releases(root: Path) -> None:
    # a binary package named with its version-release, with a requirement rpm generates, of a script
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace("%description\n", "%description\nTrivial.\n\n%package -n trivial-rel_%{version}_%{release}\n"
                 "Summary: Named with its release\n\n%description -n trivial-rel_%{version}_%{release}\n", 1)
        .replace('echo "hello" > hello.txt',
                 'echo "hello" > hello.txt\nprintf "#!/usr/bin/tclsh\\nputs hi\\n" > hi.tcl')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/\n"
                 "install -m 755 %{name}-%{version}/hi.tcl %{buildroot}/usr/share/%{name}/")
        .replace("%changelog", "%files -n trivial-rel_%{version}_%{release}\n/usr/share/%{name}/hi.tcl\n\n%changelog")
    )  # fmt: skip
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert load_builds(Project.find()).requires.of(ARCH) == {"trivial-rel_1.0_1.xcpng8.99": ["tcl"]}
    # and recorded again, from a recording of before, of a version without them
    for recording in root.glob(".xcraft/cache/builds/*/*/trivial/*/rpm-deps.json"):
        old = json.loads(recording.read_text())
        recording.write_text(json.dumps({"context": old["context"], "requires": {}, "provides": {}}))
    builds = load_builds(Project.find())
    builds.requires = Recorded()
    save_builds(Project.find(), builds)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert output.endswith("\n1 cached\n")
    assert load_builds(Project.find()).requires.of(ARCH) == {"trivial-rel_1.0_1.xcpng8.99": ["tcl"]}


def should_keep_recordings_when_a_dependent_is_rebuilt(root: Path) -> None:
    # what trivial recorded, with the former build of with-deps there, which it does not resolve to: recorded as it is,
    # rather than resolved again; with-deps records its own when built
    assert xcraft("build")[0] == 0
    (recorded,) = (root / ".xcraft/cache/builds").glob("*/*/trivial/*/rpm-deps.json")
    resolved = recorded.stat().st_mtime_ns
    edit_spec = root / "base/with-deps/with-deps.spec"
    edit_spec.write_text(f"# changed\n{edit_spec.read_text()}")
    code, output = xcraft("build")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == ["✓ with-deps built", "1 built, 1 cached"]
    assert recorded.stat().st_mtime_ns == resolved
    assert len(list((root / ".xcraft/cache/builds").glob("*/*/with-deps/*/rpm-deps.json"))) == 2


def resolutions() -> set[Path]:
    """Return the cached resolutions of the project, of all the architectures."""
    return set(Project.find().state.glob("cache/resolved/*/*.json"))


def resolved_again(monkeypatch: pytest.MonkeyPatch, *, new_process: bool = True) -> resolution.Resolved:
    """Return what the build roots of the project resolve to, prepared again, in a new process, or in this one, with
    what it read already.
    """
    if new_process:
        monkeypatch.setattr(resolution, "_resolved", {})
    resolution.prepare(project := Project.find())
    return resolution.resolve(project)


def should_resolve_build_roots_again_when_upstream_requires_our_version(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # upstream toyrun-bin, published, requires toyrun = 2-1: ours, while of that version, or else the upstream one
    write_spec(root, "toyrun")
    spec = root / "base/toyrun/toyrun.spec"
    spec.write_text(spec.read_text().replace("Version: 1.0", "Version: 2").replace("Release: 1%{?dist}", "Release: 1"))
    (root / "xcraft.toml").write_text(f'{TOML}\n[publish]\nextra = ["toyrun-bin"]\n')
    lock = load_lock(Project.find())
    lock.sources["toyrun"] = "2-1"
    save_lock(Project.find(), lock)
    assert "toyrun" not in resolved_again(monkeypatch).runtime
    spec.write_text(spec.read_text().replace("Version: 2", "Version: 3"))
    # resolved again, as toyrun-bin refers to its version
    assert "toyrun" in resolved_again(monkeypatch).runtime


VERSIONED = "trivial >= 1.0-2.xcpng8.99"
REQUIRED = ("BuildRequires: trivial", f"BuildRequires: {VERSIONED}")


@pytest.mark.parametrize(
    ("required", "published", "errors", "new_process"),
    [
        pytest.param(REQUIRED, "", "errors", True, id="build requirement"),
        pytest.param(("BuildArch: noarch", f"BuildArch: noarch\nRequires: ({VERSIONED} or no)"), "", "runtime_errors",
                     True, id="rich dependency"),
        pytest.param(("", ""), f'\n[publish]\nextra = ["{VERSIONED}"]\n', "runtime_errors", True, id="published"),
        pytest.param(REQUIRED, "", "errors", False, id="same process"),
    ],
)  # fmt: skip
def should_resolve_build_roots_again_when_a_required_version_changes(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    required: tuple[str, str],
    published: str,
    errors: str,
    *,
    new_process: bool,
) -> None:
    # what requires a version of trivial it does not have resolves again for another release, under the same key: on
    # all the architectures at once, also when the process read the resolution of the former release already
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(spec.read_text().replace(*required))
    (root / "xcraft.toml").write_text(f"{TOML}{published}")
    assert getattr(resolved_again(monkeypatch), errors)
    cached = resolutions()
    trivial = root / "base/trivial/trivial.spec"
    trivial.write_text(trivial.read_text().replace("Release: 1%{?dist}", "Release: 3%{?dist}"))
    shown: list[str] = []
    activity = resolution.activity

    def recorded(show: str) -> object:
        shown.append(show)
        return activity(show)

    monkeypatch.setattr(resolution, "activity", recorded)
    assert not getattr(resolved_again(monkeypatch, new_process=new_process), errors)
    assert resolutions() == cached
    arches = ", ".join(p.arch for p in Project.find().all_platforms())
    assert [s for s in shown if s.startswith("resolving the build roots of ")] == [
        f"resolving the build roots of {arches}, and what is published"
    ]


def should_keep_build_roots_when_subpackages_require_their_release(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # pair requires pair-libs of its own version-release, as subpackages do: another release resolves nothing again
    write_spec(root, "pair")
    spec = root / "base/pair/pair.spec"
    text = spec.read_text().replace("%description", "Requires: pair-libs = %{version}-%{release}\n%description", 1)
    text = text.replace("%files\n", "touch %{buildroot}/usr/share/doc/pair-libs\n%files\n")
    spec.write_text(
        f"{text}%package libs\nSummary: test\n%description libs\ntest\n%files libs\n/usr/share/doc/pair-libs\n"
    )
    assert "pair-libs" not in resolved_again(monkeypatch).versioned
    cached = resolutions()
    spec.write_text(spec.read_text().replace("Release: 1%{?dist}", "Release: 2%{?dist}"))
    monkeypatch.setattr(resolution, "_run", lambda *_, **__: pytest.fail("resolved again"))
    resolved_again(monkeypatch)
    assert resolutions() == cached


def should_keep_resolutions_when_their_packages_get_pinned(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # bc, needed but not locked: locked, then its binary packages pinned, which resolves nothing again
    resolved_again(monkeypatch)
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("%description", "BuildRequires: bc\n%description", 1))
    assert "bc" in resolved_again(monkeypatch).buildroots["trivial"]
    assert "bc" in load_lock(Project.find()).binaries[ARCH]
    monkeypatch.setattr(resolution, "_run", lambda *_, **__: pytest.fail("resolved again"))
    resolved_again(monkeypatch)


def should_keep_build_roots_when_a_version_has_epoch_0(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # with-deps requires the version of trivial, of epoch 0, which dnf leaves out of what it resolves
    trivial = root / "base/trivial/trivial.spec"
    trivial.write_text(trivial.read_text().replace("Version: 1.0", "Epoch: 0\nVersion: 1.0"))
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(spec.read_text().replace("BuildRequires: trivial", "BuildRequires: trivial >= 0:1.0"))
    assert "with-deps" not in resolved_again(monkeypatch).errors
    monkeypatch.setattr(resolution, "_run", lambda *_, **__: pytest.fail("resolved again"))
    assert "with-deps" not in resolved_again(monkeypatch).errors


def should_reject_an_image_when_a_base_package_is_not_in_the_repositories(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # like a base image with a package of a repository not configured: the checks of the image would reject it, unpinned
    project = Project.find()
    base = resolution.base_packages(project)
    monkeypatch.setattr(resolution, "base_packages", lambda _: [*base, "not-in-the-repositories"])
    # resolved again: the base image is in the keys of the resolutions by reference, and of those the tests share
    shutil.rmtree(project.state / "cache/resolved", ignore_errors=True)
    monkeypatch.setattr(resolution, "_resolved", {})
    monkeypatch.setattr(resolution, "_run", getattr(resolution._run, "__wrapped__", resolution._run))
    with pytest.raises(
        ResolutionError, match="not in the repositories, in the locked version: not-in-the-repositories"
    ):
        resolution.resolve_image(project)


def should_pin_the_host_image_when_the_project_is_of_other_architectures(root: Path) -> None:
    # the image of the host parses the specs, and indexes the repositories, natively
    foreign = "linux/arm64" if ARCH == "x86_64_v2" else "linux/amd64/v2"
    (root / "xcraft.toml").write_text(TOML.replace('["linux/amd64/v2", "linux/arm64"]', f'["{foreign}"]'))
    resolution.prepare(project := Project.find())
    native = project.on_host()
    lock = load_lock(project)
    assert native.arch not in {p.arch for p in project.all_platforms()}
    assert lock.image[native.arch]
    # and of its side images, which sign and lint natively
    assert lock.binaries[native.arch].keys() == {
        *lock.image[native.arch],
        *lock.sign[native.arch],
        *lock.lint[native.arch],
    }
    # kept by xcraft clean --shared
    images = container.checkout_images(project)
    assert container.image_tag(native) in images
    assert len([tag for tag in images if tag.startswith(f"{container.side_name('lint')}:")]) == 2  # of both
    assert pinned.ident(pinned.pins(native)) in container.checkout_repositories(project)


def should_rebuild_dependents_when_a_lock_update_changes_implicit_deps(root: Path) -> None:
    # a script run by toyrun, upstream, which rpm generates a requirement on
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
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert load_builds(Project.find()).requires.of(ARCH)["trivial"] == ["toyrun"]
    assert xcraft("lock", "--outdated", "toyrun") == (
        1,
        "toyrun 1-1 -> 2-1\n1 to update\nhint: xcraft lock --update updates them\n",
    )
    # through trivial, with-deps was built against toyrun: its key changes
    assert xcraft("lock", "--update", "toyrun") == (0, "toyrun 1-1 -> 2-1\nbuild IDs: with-deps 1\n1 updated\n")

    # trivial stays cached, but toyrun-bin has its interpreter now: with-deps is built against it
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == ["✓ with-deps built", "1 built, 1 cached"]
    # of the host, which may not be the first architecture, whose entries are shared
    assert load_builds(Project.find()).requires.of(ARCH)["trivial"] == ["toyrun-bin"]
    assert "toyrun-bin" in resolution.resolve(Project.find()).buildroots["with-deps"]
    assert xcraft("build", "with-deps", "--locked")[0] == 0


def should_report_the_health_of_the_host(root: Path) -> None:
    code, output = xcraft("doctor")
    assert code == 0, output
    assert output.startswith("✓ podman ")
    assert "✓ git: " in output
    # with the commands it runs, on whole lines however narrow the output
    for args in (["--debug", "doctor"], ["doctor", "--debug"]):
        cmd = [sys.executable, "-m", "xcraft", *args]
        output = subprocess.run(cmd, capture_output=True, text=True, check=True, env={**os.environ, "COLUMNS": "40"})
        podman = "podman info --format '{{.Version.Version}} {{.Host.Security.Rootless}}'"
        assert f"debug: running {podman}\n" in output.stderr


def should_apply_options_given_before_subcommands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # those of fetch and push apply to their subcommands too, given before them
    (tmp_path / "xcraft.toml").write_text(TOML)
    monkeypatch.setenv("XCRAFT_PROJECT", str(tmp_path))
    for args in (["fetch", "--arch", "sparc", "sources"], ["push", "--arch", "sparc", "builds"]):
        code, output = xcraft(*args)
        assert code == 1, output
        assert "sparc is not an architecture of the project" in output


def should_show_the_dependencies_and_dependents(root: Path) -> None:
    assert xcraft("graph", "with-deps") == (0, "with-deps\n└── trivial\n")
    assert xcraft("graph", "with-deps", "--flat") == (0, "trivial\nwith-deps\n")
    assert xcraft("graph", "trivial", "--dependents") == (0, "trivial\n└── with-deps\n")
    assert xcraft("graph", "trivial", "--dependents", "--flat") == (0, "with-deps\n")


def should_show_the_end_of_the_log_when_a_step_fails(root: Path) -> None:
    # like in CI, where the log is out of reach
    with pytest.raises(container.ContainerError, match=r"_failing\.log, which ends with:\n  first\n  boom$"):
        container.run(Project.find(), "failing", "echo first; echo boom; false")


def should_clean_the_images_of_no_checkout(root: Path) -> None:
    project, since = Project.find(), time.time()
    image = container.built_image(project)
    other = f"{container.BUILD_IMAGE}:other-{os.getpid()}"
    process.run(["podman", "tag", image, other])
    # but those of the checkout, and of the machine, of other checkouts; among which those the other tests tag
    # meanwhile, of images built in the second of `since`, as podman tells, or before
    listed = ["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"]
    machine = {tag for tag in process.run(listed, capture=True).stdout.split() if tag != other}
    found = clean.other_images(machine | container.checkout_images(project), since)
    assert other in found
    assert image not in found
    assert clean.remove_images([other]) == [other]
    assert process.run(["podman", "image", "exists", image], check=False).returncode == 0


def should_refuse_builds_when_pinned_packages_have_other_contents(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    project = Project.find()
    lock = load_lock(project)
    pin = lock.binaries[ARCH]["bash"]
    # other contents than the repositories have, of the same version: kept by the builds, which fail
    lock.binaries[ARCH]["bash"] = pin.model_copy(update={"sha256": "0" * 64})
    save_lock(project, lock)
    code, output = xcraft("build", "trivial")
    assert code == 1
    assert f"bash-{pin.evr}.{pin.arch}: the repositories have other contents, {pin.sha256}, than pinned" in output
    assert f"xcraft lock --update {pin.source} pins them" in output
    assert load_lock(project).binaries[ARCH]["bash"].sha256 == "0" * 64
    # until pinned again, on purpose
    code, output = xcraft("lock", "--update", "bash")
    assert code == 0, output
    assert load_lock(project).binaries[ARCH]["bash"] == pin
    assert "pinned again, of the same versions: bash" in output
    # a version the repositories do not have anymore, which the builds would pin again otherwise, as the lock does
    # not have it
    lock = load_lock(project)
    lock.binaries[ARCH]["bash"] = pin.model_copy(update={"evr": "0:0-0"})
    save_lock(project, lock)
    with pytest.raises(
        container.ContainerError, match=rf"bash-0:0-0\.{pin.arch}: pinned, but in no repository anymore"
    ):
        pinned.repository(Project.find())


def should_build_distinct_images_for_pins_of_other_contents(root: Path, tmp_path: Path) -> None:
    # two RPMs of a package the base image does not have, of the same name, version and architecture, but other contents
    (specs := tmp_path / "specs").mkdir()
    (specs / "toypayload.spec").write_text(
        "Name: toypayload\nVersion: 1\nRelease: 1\nSummary: test\nLicense: MIT\nBuildArch: noarch\n%description\ntest\n"
        "%install\nmkdir -p %{buildroot}/usr/share\necho %{payload} > %{buildroot}/usr/share/toypayload\n"
        "%files\n/usr/share/toypayload\n"
    )
    (rpms := tmp_path / "rpms").mkdir()
    # in the build environment, which has rpmbuild and createrepo_c
    container.run(
        Project.find(), "payloads",
        " && ".join(
            f"rpmbuild -bb --define '_topdir /tmp/{payload}' --define '_rpmdir /out/{payload}'"
            f" --define 'payload {payload}' /specs/toypayload.spec && createrepo_c -q /out/{payload}"
            for payload in ("a", "b")
        ),
        volumes=[f"{specs}:/specs:ro", f"{rpms}:/out"],
    )  # fmt: skip
    repo = tmp_path / "repo"
    shutil.copytree(rpms / "a", repo)
    config = (root / "xcraft.toml").read_text().replace('"bash", "bzip2"', '"toypayload", "bash", "bzip2"')
    (root / "xcraft.toml").write_text(f'{config}\n[repos.payload]\nbaseurl = "file://{repo}/"\ngpgcheck = false\n')
    assert xcraft("build", "--dry-run")[0] == 0
    images = []

    def payload() -> str:
        """Return the payload installed in the image, built with the layers of the cache of podman: those of the pins of
        the image only.
        """
        images.append(image := container.built_image(Project.find()))
        cat = ["podman", "run", "--rm", image, "cat", "/usr/share/toypayload"]
        return subprocess.run(cat, capture_output=True, text=True, check=True).stdout

    try:
        assert payload() == "a\n"
        # published again, pinned again on purpose
        shutil.rmtree(repo)
        shutil.copytree(rpms / "b", repo)
        code, output = xcraft("lock", "--update", "toypayload")
        assert code == 0, output
        assert "pinned again, of the same versions: toypayload" in output
        assert payload() == "b\n"
    finally:
        subprocess.run(["podman", "rmi", *images], capture_output=True, check=False)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Give the test a store of pinned packages of its own, in a volume removed after it, and return its path."""
    monkeypatch.setattr(pinned, "VOLUME", volume := f"xcraft-pinned-test-{os.getpid()}-{time.monotonic_ns()}")
    monkeypatch.setattr(pinned, "_checked", set())
    try:
        yield pinned._store()
    finally:
        subprocess.run(["podman", "volume", "rm", "-f", volume], check=False, capture_output=True)


def should_parse_the_specs_with_the_image_pins_only(root: Path, store: Path) -> None:
    # a build root with a pin the image does not have
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nBuildRequires: lzo-devel"))
    assert xcraft("build", "--dry-run")[0] == 0
    lock = load_lock(Project.find())
    pins = lock.binaries[ARCH]
    assert "lzo-devel" in pins
    # parsed again, once pinned
    shutil.rmtree(root / ".xcraft/cache/specs")
    assert xcraft("build", "--dry-run")[0] == 0
    stored = {path.name for path in (store / "rpms").glob("*/*.rpm")}
    assert stored <= {pinned.file_name(name, pins[name]) for name in lock.image[ARCH]}


@pytest.mark.usefixtures("root", "store")
def should_download_again_pinned_packages_damaged_in_the_store() -> None:
    project = Project.find()
    repo = pinned.repository(project)
    pin = load_lock(project).binaries[ARCH]["bash"]
    stored = repo.parent.parent / "rpms" / pin.sha256 / pinned.file_name("bash", pin)
    # damaged in the store after it was checked, then in the repository used again, or in another one made of it
    for remade in (False, True):
        stored.chmod(0o644)
        with stored.open("ab") as f:
            f.write(b"damaged")
        pinned._checked.clear()
        if remade:
            shutil.rmtree(repo)
        assert pinned.repository(project) == repo
        # downloaded again
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == pin.sha256
    # missing in the repository, which two commands check at once, one after the other
    (repo / pinned.file_name("bash", pin)).unlink()
    pinned._checked.clear()
    with ThreadPoolExecutor(2) as pool:
        assert list(pool.map(lambda _: pinned.repository(project), range(2))) == [repo, repo]
    assert hashlib.sha256((repo / pinned.file_name("bash", pin)).read_bytes()).hexdigest() == pin.sha256


@pytest.mark.usefixtures("root")
def should_clean_the_repositories_of_pins_no_checkout_uses(request: pytest.FixtureRequest) -> None:
    project = Project.find()
    used = {pinned.repository(project), pinned.repository(project, container._image_pins(project))}
    assert {repo.name for repo in used} <= container.checkout_repositories(project)
    store = request.getfixturevalue("store")  # of its own, once those of the checkout are used
    for repo, rpms in {"current": ["a", "b"], "old": ["b", "c"], "used": ["d"], ".partial.1": ["e"]}.items():
        (store / "repos" / repo).mkdir(parents=True)
        for rpm in rpms:
            (store / "rpms" / rpm).mkdir(parents=True, exist_ok=True)
            (store / "rpms" / rpm / f"{rpm}.rpm").touch()
            (store / "repos" / repo / f"{rpm}.rpm").hardlink_to(store / "rpms" / rpm / f"{rpm}.rpm")
    (store / "rpms/f").mkdir()
    (store / "rpms/f/f.rpm").touch()  # of no repository, like the part of a download
    since = time.time() - 3600
    for repo in ("current", "old", ".partial.1"):
        os.utime(store / "repos" / repo, (since - 1, since - 1))
    others = pinned.other_repositories({"current"}, since)
    assert sorted(others) == [".partial.1", "old"]
    pinned.remove_repositories(others)
    assert sorted(p.name for p in (store / "repos").iterdir()) == ["current", "used"]
    assert sorted(p.name for p in (store / "rpms").iterdir()) == ["a", "b", "d"]


def should_show_a_package_as_building_until_recorded(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # what rpm generated, recorded after the build container, still during the turn of the package, as xcraft status
    # shows it
    states = []
    record = builder.record_rpm_deps

    def recording(
        project: Project, names: list[str], keys: dict[str, str], *, quiet: bool = False, loose: bool = False
    ) -> bool:
        states.extend(builder.building(project, name) for name in names)
        return record(project, names, keys, quiet=quiet, loose=loose)

    monkeypatch.setattr(builder, "record_rpm_deps", recording)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert states == [True, False]
    assert not builder.building(Project.find(), "trivial")


def should_show_the_status_during_a_build(root: Path) -> None:
    assert xcraft("build", "--dry-run")[0] == 0
    with (root / ".xcraft/lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # as a build does
        assert states() == {"trivial": "missing", "with-deps": "missing"}


def should_show_the_status_by_its_short_name(root: Path) -> None:
    assert xcraft("st") == xcraft("status")


def should_export_a_patchset_during_a_build(root: Path, tmp_path: Path) -> None:
    repo = tmp_path / "trivial"
    repo.mkdir()
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "upstream"], ["tag", "1.0"]):
        git(repo, *args)
    (repo / "fix").write_text("fix\n")
    git(repo, "add", "fix")
    git(repo, "commit", "-qm", "a fix")
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("%prep\n", "%prep\n%autopatch -p1\n"))
    with (root / ".xcraft/lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # as a build does
        # the package of the repository by its name, and its version from its spec only, without the lock
        code, output = xcraft("patchset", str(repo))
        assert code == 0, output
    assert output.startswith("trivial: patchset from 1.0, 1 added")
    assert (root / "base/trivial/0001-a-fix.patch").exists()


def should_lock_the_image_and_the_build_roots(root: Path) -> None:
    spec = root / "base/trivial/trivial.spec"
    original = spec.read_text()
    spec.write_text(original.replace("BuildArch: noarch", "BuildArch: noarch\nBuildRequires: lzo-devel"))
    assert xcraft("build", "--dry-run")[0] == 0
    lock = load_lock(Project.find())
    image = set(lock.image[ARCH])
    assert "glibc" in image
    # the upstream binary packages of the build roots, but the image's
    assert "lzo-devel" in lock.roots.shared["trivial"]
    assert not image & {*lock.roots.shared["trivial"], *lock.roots.shared.get("with-deps", [])}
    # without ccache, which only compilers get
    assert "ccache" not in lock.roots.shared["trivial"]
    # recorded again when they change, which --locked forbids
    spec.write_text(original)
    code, output = xcraft("build", "--dry-run", "--locked")
    assert code == 1
    assert "--locked forbids: " in output
    assert "-roots.trivial lzo" in output
    assert xcraft("build", "--dry-run")[0] == 0
    assert "lzo" not in load_lock(Project.find()).roots.shared.get("trivial", [])


def should_drop_the_recordings_of_packages_removed_from_the_project(root: Path) -> None:
    gone = 'gone.requires."gone" = ["bash"]\ngone.aarch64.provides."gone" = ["zsh"]\ngone.buildrequires = ["bash"]\n'
    with builds_path(Project.find()).open("a") as f:
        f.write(f"\n# gone\n{gone}")
    assert xcraft("build", "--dry-run")[0] == 0
    assert "gone" not in builds_path(Project.find()).read_text()
