"""Builds in the real build environment, on the fixture packages."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests import ARCH, TOML, TOYS, copy_project
from xcraft import build as builder
from xcraft import buildid, checks, clean, container, localrepo, pinned, process, publish, resolution
from xcraft.__main__ import app
from xcraft.builds import builds_path, load_builds, save_builds
from xcraft.cache import BuildError
from xcraft.iso import _INSTALL_TOOLS, _check_installed_tools
from xcraft.localrepo import LocalRepo
from xcraft.lock import Recorded, load_lock, lock_path, save_lock
from xcraft.project import Project
from xcraft.publish import _check_installable
from xcraft.resolution import ISO_TOOLS, ResolutionError
from xcraft.signing import check_signatures
from xcraft.sources import write_manifest

runner = CliRunner()


# in the build environment, whose image is built the first time, which needs the network: see the podman marker


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_environment: Path) -> Path:
    root = tmp_path / "project"
    copy_project(root, build_environment)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


def results(output: str) -> list[str]:
    """Return the lines of the results of a build, without its errors and warnings."""
    return [line for line in output.splitlines() if line.startswith(("✓", "✗", "-", "↓")) or line[:1].isdigit()]


def xcraft(*args: str) -> tuple[int, str]:
    """Run xcraft, returning its exit code and output, including the error main() would show."""
    result = runner.invoke(app, list(args))
    error = result.exception if result.exception and not isinstance(result.exception, SystemExit) else ""
    return result.exit_code, f"{result.output}{error}"


def states() -> dict[str, str]:
    """Return the state of each package shown by xcraft status."""
    return {line.split()[0]: line.split()[2] for line in xcraft("status")[1].splitlines()[1:-1]}


def test_build_uses_local_builds_and_cache(root: Path) -> None:
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
    assert list((root / f".xcraft/cache/builds/{ARCH}/with-deps").glob("*/RPMS/noarch/with-deps-1.0-*.noarch.rpm"))

    # a change in trivial changes the key of with-deps too
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello again"'))
    assert states() == {"trivial": "outdated", "with-deps": "outdated"}
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert output.splitlines()[-1] == "2 built"
    assert states() == {"trivial": "built", "with-deps": "built"}


def test_build_all_by_default(root: Path) -> None:
    code, output = xcraft("build", "--dry-run")
    assert code == 0, output
    assert output.splitlines() == ["build    trivial", "build    with-deps", "2 to build"]
    assert xcraft("build", "-n") == (code, output)


def test_spec_changed_during_build_is_not_cached(root: Path) -> None:
    spec = root / "base/trivial/trivial.spec"

    def change() -> None:
        for _ in range(1200):  # not after the test, whose build fails to start
            if (root / ".xcraft/logs/trivial.log").exists():  # its build started, staged
                spec.write_text(f"{spec.read_text()}# changed\n")
                return
            time.sleep(0.1)

    threading.Thread(target=change, daemon=True).start()
    code, output = xcraft("build", "trivial")
    assert code == 1
    assert "base/trivial changed during the build, build it again" in output
    assert states() == {"trivial": "missing", "with-deps": "missing"}


def test_spec_changed_before_its_build_is_not_cached(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


HOST_ENTRIES = "" if ARCH == "x86_64_v2" else f"{ARCH}."
"""The prefix of the entries of the lock the builds on the host record, not shared but on the first architecture."""
UP_TO_DATE = "versions up to date, not looking for other contents of the same versions: xcraft lock --update does"


def write_spec(root: Path, name: str, requires: str = "", files: str = "/usr/share/doc/%{name}") -> None:
    """Write a minimal noarch package installing `files`, with the BuildRequires `requires`."""
    (root / "base" / name).mkdir()
    (root / "base" / name / f"{name}.spec").write_text(
        f"Name: {name}\nVersion: 1.0\nRelease: 1%{{?dist}}\nSummary: test\nLicense: MIT\nBuildArch: noarch\n"
        + (f"BuildRequires: {requires}\n" if requires else "")
        + f"%description\ntest\n%install\nmkdir -p %{{buildroot}}{files.rsplit('/', 1)[0]}\n"
        + f"touch %{{buildroot}}{files}\n%files\n{files}\n"
    )


def test_binary_of_two_packages(root: Path) -> None:
    write_spec(root, "twin")
    spec = root / "base/twin/twin.spec"
    spec.write_text(spec.read_text().replace("Name: twin", "Name: trivial"))
    code, output = xcraft("build", "--dry-run")
    assert code == 1
    # on the architecture parsed first, maybe not the host's
    assert re.search(r"binary packages of several packages for \S+: trivial \(trivial, twin\)", output)


def test_build_without_network(root: Path) -> None:
    write_spec(root, "offline")
    spec = root / "base/offline/offline.spec"
    check = (
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
    spec.write_text(spec.read_text().replace("%install\n", f"%build\npython3 - <<'EOF'\n{check}EOF\n%install\n"))
    code, output = xcraft("build", "offline")
    assert code == 0, output
    assert "no network" in (root / ".xcraft/logs/offline.log").read_text()  # but its loopback interface


def test_build_killed_by_closed_pipes(root: Path) -> None:
    write_spec(root, "pipes")
    spec = root / "base/pipes/pipes.spec"
    check = "seq 1000000 2>seq.err | head -1\ntest ! -s seq.err || { cat seq.err; exit 1; }\n"
    spec.write_text(spec.read_text().replace("%install\n", f"%build\n{check}%install\n"))
    code, output = xcraft("build", "pipes")
    assert code == 0, output


def test_dynamic_buildrequires_on_ours(root: Path) -> None:
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(spec.read_text().replace("BuildRequires: trivial\n", "").replace(
        "%prep", "%generate_buildrequires\necho trivial\n\n%prep"
    ))  # fmt: skip
    # not known to need trivial: built alone, then again after it
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    assert "↻ with-deps: " in output
    assert [line.split(" built")[0] for line in results(output) if line[0] == "✓"] == ["✓ trivial", "✓ with-deps"]
    assert load_builds(Project.find()).buildrequires.of(ARCH) == {"with-deps": ["trivial"]}


def test_build_root_of_files_of_ours(root: Path) -> None:
    # xcraft does not know which files our packages provide before they are built
    write_spec(root, "tool", files="/usr/bin/xcraft-test-tool")
    write_spec(root, "tool-user", "/usr/bin/xcraft-test-tool")
    code, output = xcraft("build", "tool-user")
    assert code == 1
    assert "its build root cannot be resolved: nothing provides /usr/bin/xcraft-test-tool" in output
    # then the build of tool records it
    assert xcraft("build", "tool")[0] == 0
    code, output = xcraft("build", "tool-user")
    assert code == 0, output
    assert load_builds(Project.find()).provides.of(ARCH) == {"tool": ["/usr/bin/xcraft-test-tool"]}
    # upstream's bc, instead of ours, never built since nothing is known to need it
    write_spec(root, "bc")
    write_spec(root, "bc-user", "/usr/bin/bc")
    code, output = xcraft("build", "bc-user")
    assert code == 1
    assert "its build root has the upstream bc (ours from bc)" in output


def test_build_root_packages_replaced_by_ours(root: Path) -> None:
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
    assert "kernel-headers" in load_lock(project).roots.of(ARCH)["compiled"]
    assert re.search(r"^ kernel-headers .* xcraft-[0-9a-f]{8} ", project.log("compiled").read_text(), re.MULTILINE)


def test_image_packages_replaced_by_ours(root: Path) -> None:
    # openblas-srpm-macros is in the image, and harmless to replace, by one requiring a pinned package it does not have
    write_spec(root, "openblas-srpm-macros", files="/usr/share/xcraft-test/openblas-srpm-macros")
    spec = root / "base/openblas-srpm-macros/openblas-srpm-macros.spec"
    spec.write_text(spec.read_text().replace("Summary: test", "Requires: toyrun\nSummary: test"))
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert [line.split(" built")[0] for line in results(output)[:2]] == ["✓ openblas-srpm-macros", "✓ trivial"]
    log = root.joinpath(".xcraft/logs/trivial.log").read_text()
    assert re.search(r"^Downgrading:\n openblas-srpm-macros .* xcraft-[0-9a-f]{8} ", log, re.MULTILINE)
    assert re.search(r"^ toyrun .* xcraft-pinned ", log, re.MULTILINE), log


def test_build_ids_without_building(root: Path) -> None:
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


def test_repo_host_options(root: Path) -> None:
    for args, error in [
        (["--host", "h", "--final"], "--host, --vm and --upgrade are for the development builds of the given packages"),
        (["--upgrade"], "--upgrade needs --host or --vm"),
        (["--host", "h", "--baseurl", "http://x/"], "with --host or --vm, the hosts find the repository in"),
        (["--host", "ssh://root@h:port"], "not a host: ssh://root@h:port, as ssh://[user@]host[:port]"),
        (["--vm"], "no VM running in"),
    ]:
        code, output = xcraft("repo", "trivial", *args)
        assert code == 2
        assert error in " ".join(output.replace("│", "").split())


def test_dnf_lock_is_shared_by_containers(root: Path) -> None:
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


def test_build_back_to_a_cached_key(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    # dynamic BuildRequires its build does not find: missing, and built again, back to the key of the first build
    builds = load_builds(Project.find())
    builds.buildrequires.shared["trivial"] = ["zlib-ng-compat-devel"]
    save_builds(Project.find(), builds)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert results(output) == [results(output)[0], "1 built"]
    assert states() == {"trivial": "built", "with-deps": "missing"}
    assert not list((root / f".xcraft/cache/builds/{ARCH}/trivial").glob(".*"))


def test_failed_build_skips_dependents(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_missing_archive_and_spec_error(root: Path) -> None:
    write_manifest(root / "base/trivial/sources", {"trivial-1.0.tar.gz": "0" * 64})
    (root / "base/broken").mkdir()
    (root / "base/broken/broken.spec").write_text("Name: broken\n%if\n")
    code, output = xcraft("build", "trivial", "broken")
    assert code == 1
    assert "trivial-1.0.tar.gz is not in the source cache, the remote storage, nor at a URL of the spec" in output
    assert "✗ broken: " in output
    assert re.search(r"^broken +error *$", xcraft("status")[1], re.MULTILINE)
    assert xcraft("st") == xcraft("status")


def test_repo_checks_dependencies(root: Path) -> None:
    code, output = xcraft("repo", "with-deps")
    assert code == 0, output
    assert f"repository in {root / 'repo'}, with all dependencies satisfied" in output
    assert sorted(p.name.split("-1.0")[0] for p in (root / "repo").glob("*.rpm")) == ["trivial", "with-deps"]
    assert (root / "repo/repodata/repomd.xml").exists()
    # with the cached metadata and builds, after clean removed .xcraft/tmp, what a dry run shows only
    code, output = xcraft("clean", "-n")
    assert code == 0, output
    assert re.search(r"^local repositories +1 to remove$", output, re.MULTILINE), output
    assert (root / ".xcraft/repo").exists()
    code, output = xcraft("clean")
    assert code == 0, output
    assert re.search(r"^local repositories +1 removed$", output, re.MULTILINE), output
    assert not (root / ".xcraft/repo").exists()
    code, output = xcraft("repo", "with-deps")
    assert code == 0, output

    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nRequires: no-such-package"))
    code, output = xcraft("repo", "trivial", "--output", str(root / "out"))
    assert code == 1
    assert "no-such-package" in output
    assert "some dependencies cannot be satisfied" in output


def test_bump(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gitconfig = root / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Me\n\temail = me@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
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
    assert xcraft("bump")[0] == 2  # no package


def test_bump_version(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gitconfig = root / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Me\n\temail = me@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
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
    # without a URL to download the archive from, nothing changes
    before = (root / "base/with-deps/with-deps.spec").read_text()
    code, output = xcraft("bump", "with-deps", "--version", "2.0", "-m", "Update")
    assert code == 1
    assert "the Source0 of with-deps is not a URL" in output
    assert (root / "base/with-deps/with-deps.spec").read_text() == before


@pytest.mark.parametrize("version", ["1.9", "1.10", "1.010", "1.10~rc1"])
def test_bump_version_must_increase(root: Path, monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    gitconfig = root / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Me\n\temail = me@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
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


@pytest.mark.parametrize("output", ["yes looping", "yes looping | tr -d '\\n'"])  # in lines, or in a single one
def test_runaway_log_stops_the_build(output: str, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "MAX_LOG_SIZE", 2**20)
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello" > hello.txt', output))
    code, output = xcraft("build", "trivial")
    assert code == 1
    assert "✗ trivial: stopped, its log got larger than 1 MiB" in output
    name = f"xcraft-{Project.find().id}-trivial"
    assert subprocess.run(["podman", "container", "exists", name], check=False).returncode != 0


def test_lock(root: Path) -> None:
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
    assert xcraft("lock", "--outdated", "lzo") == (0, f"{UP_TO_DATE}\n")


def test_lock_versions_of_all_the_architectures(root: Path) -> None:
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
    expected = "toyrun 1-1 -> 2-1\n1 to update: xcraft lock --update updates them\n"
    assert xcraft("lock", "--outdated", "toyarch", "toyrun") == (1, expected)
    assert load_lock(Project.find()).sources["toyrun"] == "1-1"
    assert xcraft("lock", "--update", "toyarch", "toyrun") == (0, "toyrun 1-1 -> 2-1\n1 updated\n")
    assert load_lock(Project.find()).sources["toyrun"] == "2-1"


def test_lock_records_runtime(root: Path) -> None:
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
def test_iso_tools(root: Path) -> None:
    (root / "xcraft.toml").write_text(f'{TOML}installer = ["bash"]\n')
    assert xcraft("build", "--dry-run")[0] == 0
    project = Project.find()
    lock = load_lock(project)
    # pinned when preparing, with what the installer needs
    assert set(ISO_TOOLS) <= set(lock.iso[project.arch]) <= lock.binaries[project.arch].keys()
    assert {"bash", "glibc"} <= set(lock.runtime[project.arch]) <= lock.binaries[project.arch].keys()
    assert {"dosfstools", "libisoburn", "memtest86+", "mtools", "syslinux"} <= lock.sources.keys()  # by source
    assert lock.iso.keys() == {project.arch}
    # installed from the pinned packages
    with project.tmp_dir("tools") as work:
        record = f"{container.DNF_LOCK} python3 /xcraft/record.py /work/tools.json"
        container.run(project, "tools", f"{_INSTALL_TOOLS} && {record}", volumes=[f"{work}:/work"], install=True)
        _check_installed_tools(project, work / "tools.json")
        del lock.iso[project.arch][0]
        save_lock(project, lock)
        with pytest.raises(RuntimeError, match="the tools making the ISO are not those pinned in the lock: "):
            _check_installed_tools(project, work / "tools.json")


@pytest.mark.skipif(ARCH != "x86_64_v2", reason="the ISOs are for x86_64")
def test_iso_packages_published(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # bc, which nothing else needs, in the repository of the ISO: published, for the snapshot to have it
    iso = 'installer = ["bash"]\npackages = ["bc"]\n'
    (root / "xcraft.toml").write_text(f"{TOML}{iso}")
    assert xcraft("build", "--dry-run")[0] == 0
    lock = load_lock(Project.find())
    assert "bc" in lock.runtime[ARCH]
    assert "bc" not in lock.runtime["aarch64"]
    # the tools making the ISO, which cannot be installed, kept as the lock has them, but not what is published
    monkeypatch.setattr(resolution, "ISO_TOOLS", [*ISO_TOOLS, "no-such-package"])
    (root / "xcraft.toml").write_text(f'{TOML}{iso}\n[publish]\nextra = ["toyrun"]\n')
    assert xcraft("build", "--dry-run")[0] == 0
    updated = load_lock(Project.find())
    assert "toyrun" in updated.runtime[ARCH]
    assert updated.iso == lock.iso


def test_recorded_requires_of_the_image(root: Path) -> None:
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


def test_snapshot_of_a_checkout_changed_while_making_it(
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


def test_snapshots_checked_in_turn(root: Path, tmp_path: Path) -> None:
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


@pytest.mark.parametrize("cached", [False, True])
def test_recorded_once_all_built(root: Path, *, cached: bool) -> None:
    # a script of trivial run by xtool, of ours too, built after it: recorded once xtool is built, without warning
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
    if cached:
        code, output = xcraft("build", "trivial")
        assert code == 0, output
        assert "cannot record what rpm generated for trivial" in output
    code, output = xcraft("build", "trivial", "xtool")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == (
        ["✓ xtool built", "1 built, 1 cached"] if cached else ["✓ trivial built", "✓ xtool built", "2 built"]
    )
    assert "cannot record" not in output
    assert load_builds(Project.find()).requires.of(ARCH)["trivial"] == ["xtool"]
    assert xcraft("build", "trivial", "xtool", "--locked") == (0, "2 cached\n")


def test_dune_cache(root: Path) -> None:
    # the cache of dune of trivial, which writes in it like dune: kept on this machine, but for the builds without dune
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text().replace(
            'echo "hello" > hello.txt',
            'echo "hello" > hello.txt\necho "$DUNE_CACHE $DUNE_CACHE_STORAGE_MODE" > "$DUNE_CACHE_ROOT/entry"',
        )
    )
    code, output = xcraft("build")
    assert code == 0, output
    caches = root / ".xcraft/dune-cache"
    assert (caches / "trivial/entry").read_text() == "enabled copy\n"
    assert [p.name for p in caches.iterdir()] == ["trivial"]  # not with-deps, without dune
    assert xcraft("clean", "trivial")[0] == 0
    assert (caches / "trivial/entry").exists()
    assert xcraft("clean", "trivial", "--dune-cache")[0] == 0
    assert not (caches / "trivial").exists()


def test_recorded_once_what_they_require_is_built(root: Path) -> None:
    # trivial requires what xtool, of ours, provides: recorded once xtool is built, without the problem of dnf
    (root / "base/xtool").mkdir()
    (root / "base/xtool/xtool.spec").write_text(
        "Name: xtool\nVersion: 1.0\nRelease: 1%{?dist}\nSummary: A tool\nLicense: MIT\nBuildArch: noarch\n"
        "Provides: xtool-core\n%description\nA tool.\n%install\nmkdir -p %{buildroot}/usr/share/xtool\n"
        "touch %{buildroot}/usr/share/xtool/core\n%files\n/usr/share/xtool\n"
    )
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nRequires: xtool-core"))
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert (
        "warning: the dependencies rpm generated for trivial are recorded once xtool, which it needs, is built"
        in output
    )
    assert "cannot record" not in output
    code, output = xcraft("build", "trivial", "xtool")
    assert code == 0, output
    assert "recorded once" not in output
    assert load_builds(Project.find()).requires.of(ARCH).get("trivial") is None  # what it requires, declared


def test_recorded_as_soon_as_resolved(root: Path) -> None:
    # trivial, whose script requires tcl, recorded at once, and trivial-extra, which requires what xtool, of ours,
    # provides, once xtool is built: the builds against trivial get tcl before
    (root / "base/xtool").mkdir()
    (root / "base/xtool/xtool.spec").write_text(
        "Name: xtool\nVersion: 1.0\nRelease: 1%{?dist}\nSummary: A tool\nLicense: MIT\nBuildArch: noarch\n"
        "Provides: xtool-core\n%description\nA tool.\n%install\nmkdir -p %{buildroot}/usr/share/xtool\n"
        "touch %{buildroot}/usr/share/xtool/core\n%files\n/usr/share/xtool\n"
    )
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace('echo "hello" > hello.txt', 'printf "#!/usr/bin/tclsh\\nputs hello\\n" > hello.tcl')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "install -m 755 %{name}-%{version}/hello.tcl %{buildroot}/usr/share/%{name}/\n"
                 "touch %{buildroot}/usr/share/%{name}/extra")
        .replace("%files\n/usr/share/%{name}/hello.txt",
                 "%package extra\nSummary: Extra\nRequires: xtool-core\n%description extra\nExtra.\n"
                 "%files extra\n/usr/share/%{name}/extra\n\n%files\n/usr/share/%{name}/hello.tcl")
    )  # fmt: skip
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert (
        "warning: the dependencies rpm generated for trivial-extra are recorded once xtool, which it needs, is built"
        in output
    )
    assert load_builds(Project.find()).requires.of(ARCH) == {"trivial": ["tcl"]}
    assert "tcl" in resolution.resolve(Project.find()).buildroots["with-deps"]
    code, output = xcraft("build", "trivial", "xtool", "with-deps")
    assert code == 0, output
    assert "recorded once" not in output
    assert load_builds(Project.find()).requires.of(ARCH) == {"trivial": ["tcl"]}


def test_recorded(root: Path) -> None:
    # a requirement rpm generates, of a script, and dynamic BuildRequires
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace("%prep", "%generate_buildrequires\necho zlib-ng-compat-devel\n\n%prep")
        .replace('echo "hello" > hello.txt', 'printf "#!/usr/bin/tclsh\\nputs hello\\n" > hello.tcl')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "install -m 755 %{name}-%{version}/hello.tcl %{buildroot}/usr/share/%{name}/")
        .replace("/usr/share/%{name}/hello.txt", "/usr/share/%{name}/hello.tcl")
    )  # fmt: skip
    buildid.update(Project.find())  # the build IDs of the changed spec, --locked checks before building
    code, output = xcraft("build", "trivial", "--locked")
    assert code == 1
    # those of the architecture of the host, which are shared once found on the first one
    assert f'+trivial.{HOST_ENTRIES}buildrequires = ["zlib-ng-compat-devel"]' in output
    code, output = xcraft("build", "with-deps")
    assert code == 0, output
    builds = load_builds(Project.find())
    assert builds.buildrequires.of(ARCH) == {"trivial": ["zlib-ng-compat-devel"]}
    # which with-deps gets through trivial, its build root checked
    assert builds.requires.of(ARCH) == {"trivial": ["tcl"]}
    assert "tcl" in resolution.resolve(Project.find()).buildroots["with-deps"]
    assert xcraft("build", "with-deps", "--locked")[0] == 0


def test_names_with_their_releases_by_chance(root: Path) -> None:
    # the digit of its release in its name, without %dist: not named after its release
    write_spec(root, "libfoo1")
    spec = root / "base/libfoo1/libfoo1.spec"
    spec.write_text(spec.read_text().replace("Release: 1%{?dist}", "Release: 1%{?autorev}"))
    code, output = xcraft("build", "libfoo1")
    assert code == 0, output
    assert [p.name for p in root.glob(".xcraft/cache/builds/*/libfoo1/*/RPMS/*/*.rpm")] == [
        "libfoo1-1.0-1+b1.noarch.rpm"
    ]


def test_recorded_of_binaries_named_with_their_releases(root: Path) -> None:
    # like the livepatch packages of the kernel, with a requirement rpm generates, of a script
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text()
        .replace("%description\n", "%description\nTrivial.\n\n%package -n trivial-lp_%{version}_%{release}\n"
                 "Summary: Named with its release\n\n%description -n trivial-lp_%{version}_%{release}\n", 1)
        .replace('echo "hello" > hello.txt',
                 'echo "hello" > hello.txt\nprintf "#!/usr/bin/tclsh\\nputs hi\\n" > hi.tcl')
        .replace("cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/",
                 "cp %{name}-%{version}/hello.txt %{buildroot}/usr/share/%{name}/\n"
                 "install -m 755 %{name}-%{version}/hi.tcl %{buildroot}/usr/share/%{name}/")
        .replace("%changelog", "%files -n trivial-lp_%{version}_%{release}\n/usr/share/%{name}/hi.tcl\n\n%changelog")
    )  # fmt: skip
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert load_builds(Project.find()).requires.of(ARCH) == {"trivial-lp_1.0_1.xcpng8.99": ["tcl"]}
    # and recorded again, from a recording of before, of a version without them
    for recording in root.glob(".xcraft/cache/builds/*/trivial/*/rpm-deps.json"):
        old = json.loads(recording.read_text())
        recording.write_text(json.dumps({"context": old["context"], "requires": {}, "provides": {}}))
    builds = load_builds(Project.find())
    builds.requires = Recorded()
    save_builds(Project.find(), builds)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert output.endswith("\n1 cached\n")
    assert load_builds(Project.find()).requires.of(ARCH) == {"trivial-lp_1.0_1.xcpng8.99": ["tcl"]}


def test_recorded_once_after_a_change(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # what trivial recorded, with the former build of with-deps there, which it does not resolve to: recorded as it is,
    # rather than resolved again; with-deps records its own when built
    assert xcraft("build")[0] == 0
    edit_spec = root / "base/with-deps/with-deps.spec"
    edit_spec.write_text(f"# changed\n{edit_spec.read_text()}")
    resolved: list[list[str]] = []
    resolve = checks.resolve_rpm_deps

    def counted(project: Project, names: dict[str, str], rpms: list[Path]) -> resolution.Resolved:
        resolved.append(sorted(names.values()))
        return resolve(project, names, rpms)

    monkeypatch.setattr(checks, "resolve_rpm_deps", counted)
    code, output = xcraft("build")
    assert code == 0, output
    assert [line.split(" in ")[0] for line in results(output)] == ["✓ with-deps built", "1 built, 1 cached"]
    assert resolved == [["with-deps"]]


def test_build_roots_kept_for_another_release(root: Path) -> None:
    def key() -> Path:
        project = Project.find()
        return resolution._cache(project, resolution._unversioned(project, resolution._roots_data(project)))

    def release(name: str, number: int) -> None:
        spec = root / f"base/{name}/{name}.spec"
        spec.write_text(re.sub(r"^Release: \d+", f"Release: {number}", spec.read_text(), flags=re.MULTILINE))

    # another release of trivial, whose version with-deps does not compare: the same build roots
    first = key()
    release("trivial", 2)
    assert key() == first


def _roots_key() -> Path:
    """Return the cache of the build roots of the project, as looked up, with the lock as it is."""
    project = Project.find()
    return resolution._cache(project, resolution._unversioned(project, resolution._roots_data(project)))


def _resolved_again(monkeypatch: pytest.MonkeyPatch, *, new_process: bool = True) -> tuple[Path, resolution.Resolved]:
    """Return the cache of the build roots of the project looked up, before what the resolution locks, and what they
    resolve to, in a new process, or in this one, with what it read already.
    """
    if new_process:
        monkeypatch.setattr(resolution, "_resolved", {})
    cache = _roots_key()
    resolution.prepare(project := Project.find())
    return cache, resolution.resolve(project)


def test_build_roots_resolved_again_for_versions_upstream_requires(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # upstream toyrun-bin, published, requires toyrun = 2-1: ours, while of that version, or else the upstream one
    write_spec(root, "toyrun")
    spec = root / "base/toyrun/toyrun.spec"
    spec.write_text(spec.read_text().replace("Version: 1.0", "Version: 2").replace("Release: 1%{?dist}", "Release: 1"))
    (root / "xcraft.toml").write_text(f'{TOML}\n[publish]\nextra = ["toyrun-bin"]\n')
    lock = load_lock(Project.find())
    lock.sources["toyrun"] = "2-1"
    save_lock(Project.find(), lock)
    resolved = _resolved_again(monkeypatch)[1]
    assert "toyrun" not in resolved.runtime
    cache = _roots_key()  # with what the resolution locked
    spec.write_text(spec.read_text().replace("Version: 2", "Version: 3"))
    # the same key, without the versions of ours, but resolved again, as toyrun-bin refers to its version
    again, resolved = _resolved_again(monkeypatch)
    assert again == cache
    assert "toyrun" in resolved.runtime


def test_build_roots_resolved_again_for_versions_of_build_requirements(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(spec.read_text().replace("BuildRequires: trivial", "BuildRequires: trivial >= 1.0-2.xcpng8.99"))
    resolved = _resolved_again(monkeypatch)[1]
    assert "with-deps" in resolved.errors
    cache = _roots_key()
    trivial = root / "base/trivial/trivial.spec"
    trivial.write_text(trivial.read_text().replace("Release: 1%{?dist}", "Release: 3%{?dist}"))
    again, resolved = _resolved_again(monkeypatch)
    assert again == cache
    assert "with-deps" not in resolved.errors


def test_image_needs_the_packages_of_the_base_image_in_the_repositories(
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


def test_image_of_the_host_pinned_for_a_project_of_other_architectures(root: Path) -> None:
    # the image of the host parses the specs, and indexes the repositories, natively
    foreign = "linux/arm64" if ARCH == "x86_64_v2" else "linux/amd64/v2"
    (root / "xcraft.toml").write_text(TOML.replace('["linux/amd64/v2", "linux/arm64"]', f'["{foreign}"]'))
    resolution.prepare(project := Project.find())
    native = project.on_host()
    lock = load_lock(project)
    assert native.arch not in {p.arch for p in project.all_platforms()}
    assert lock.image[native.arch]
    assert lock.binaries[native.arch].keys() == {*lock.image[native.arch]}
    assert container._image_pins(native)
    # kept by xcraft clean --shared
    assert container.image_tag(native) in container.checkout_images(project)
    assert pinned.ident(container._image_pins(native)) in container.checkout_repositories(project)


def test_build_roots_resolved_again_for_versions_of_rich_dependencies(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(
        spec.read_text().replace(
            "BuildArch: noarch", "BuildArch: noarch\nRequires: (trivial >= 1.0-2.xcpng8.99 or nonexistent)"
        )
    )
    resolved = _resolved_again(monkeypatch)[1]
    assert resolved.runtime_errors
    cache = _roots_key()
    trivial = root / "base/trivial/trivial.spec"
    trivial.write_text(trivial.read_text().replace("Release: 1%{?dist}", "Release: 2%{?dist}"))
    again, resolved = _resolved_again(monkeypatch)
    assert again == cache
    assert not resolved.runtime_errors


def test_build_roots_resolved_again_for_versions_published(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "xcraft.toml").write_text(f'{TOML}\n[publish]\nextra = ["trivial >= 1.0-2.xcpng8.99"]\n')
    resolved = _resolved_again(monkeypatch)[1]
    assert resolved.runtime_errors
    cache = _roots_key()
    trivial = root / "base/trivial/trivial.spec"
    trivial.write_text(trivial.read_text().replace("Release: 1%{?dist}", "Release: 2%{?dist}"))
    again, resolved = _resolved_again(monkeypatch)
    assert again == cache
    assert not resolved.runtime_errors


def test_build_roots_resolved_again_in_the_same_process(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # what the process read of the resolution of the former version, kept in memory, does not hold either; of a
    # version no release of trivial has, the same key
    spec = root / "base/with-deps/with-deps.spec"
    spec.write_text(spec.read_text().replace("BuildRequires: trivial", "BuildRequires: trivial >= 1.0-2"))
    assert "with-deps" in _resolved_again(monkeypatch)[1].errors
    cache = _roots_key()
    trivial = root / "base/trivial/trivial.spec"
    trivial.write_text(trivial.read_text().replace("Release: 1%{?dist}", "Release: 2%{?dist}"))
    again, resolved = _resolved_again(monkeypatch, new_process=False)
    assert again == cache
    assert "with-deps" not in resolved.errors


def test_lock_update_changing_implicit_deps(root: Path) -> None:
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
        "toyrun 1-1 -> 2-1\n1 to update: xcraft lock --update updates them\n",
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


def test_doctor(root: Path) -> None:
    code, output = xcraft("doctor")
    assert code == 0, output
    assert output.startswith("✓ podman ")
    assert "✓ git: " in output
    # with the commands it runs, on whole lines however narrow the output
    for args in (["--debug", "doctor"], ["doctor", "--debug"]):
        cmd = [sys.executable, "-m", "xcraft", *args]
        output = subprocess.run(cmd, capture_output=True, text=True, check=True, env={**os.environ, "COLUMNS": "40"})
        assert "running podman info --format '{{.Version.Version}} {{.Host.Security.Rootless}}'\n" in output.stdout


def test_options_before_subcommands(root: Path) -> None:
    # those of fetch and push apply to their subcommands too, given before them
    for args in (["fetch", "--arch", "sparc", "sources"], ["push", "--arch", "sparc", "builds"]):
        code, output = xcraft(*args)
        assert code == 1, output
        assert "sparc is not an architecture of the project" in output


def test_graph(root: Path) -> None:
    assert xcraft("graph", "with-deps") == (0, "with-deps\n└── trivial\n")
    assert xcraft("graph", "with-deps", "--flat") == (0, "trivial\nwith-deps\n")
    assert xcraft("graph", "trivial", "--dependents") == (0, "trivial\n└── with-deps\n")
    assert xcraft("graph", "trivial", "--dependents", "--flat") == (0, "with-deps\n")


def test_failed_step_shows_its_log(root: Path) -> None:
    # like in CI, where the log is out of reach
    with pytest.raises(container.ContainerError, match=r"_failing\.log, which ends with:\n  first\n  boom$"):
        container.run(Project.find(), "failing", "echo first; echo boom; false")


def test_clean_removes_the_other_images(root: Path) -> None:
    project, since = Project.find(), time.time()
    image = container.built_image(project)
    other = f"{container.BUILD_IMAGE}:other-{os.getpid()}"
    process.run(["podman", "tag", image, other])
    # but those of the machine, of other checkouts, and those the other tests build since
    listed = ["podman", "images", "--format", "{{.Repository}}:{{.Tag}}"]
    machine = {tag for tag in process.run(listed, capture=True).stdout.split() if tag != other}
    assert clean.other_images(machine | container.checkout_images(project), since) == [other]
    assert clean.remove_images([other]) == [other]
    assert process.run(["podman", "image", "exists", image], check=False).returncode == 0


def test_pinned_packages(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    project = Project.find()
    lock, tag = load_lock(project), container.image_tag(project)
    pin = lock.binaries[ARCH]["bash"]
    # other contents than the repositories have, of the same version: kept by the builds, which fail, in another image
    lock.binaries[ARCH]["bash"] = pin.model_copy(update={"sha256": "0" * 64})
    save_lock(project, lock)
    assert container.image_tag(Project.find()) != tag
    # with its pins before installing them, not a layer of the cache of podman of other contents
    containerfile = (container.BUILDENV / "Containerfile").read_text()
    assert containerfile.index("COPY pins.list") < containerfile.index("distro-sync")
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


def test_images_of_other_contents(root: Path, tmp_path: Path) -> None:
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
    (root / "xcraft.toml").write_text(f'{config}\n[repos.payload]\nbaseurl = "file://{repo}/"\n')
    assert xcraft("build", "--dry-run")[0] == 0
    images = []
    for payload in ("a", "b"):
        if payload == "b":  # published again, pinned again on purpose
            shutil.rmtree(repo)
            shutil.copytree(rpms / "b", repo)
            code, output = xcraft("lock", "--update", "toypayload")
            assert code == 0, output
            assert "pinned again, of the same versions: toypayload" in output
        images.append(image := container.built_image(Project.find()))
        # with the layers of the cache of podman: those of the pins of the image only
        cat = ["podman", "run", "--rm", image, "cat", "/usr/share/toypayload"]
        installed = subprocess.run(cat, capture_output=True, text=True, check=True)
        assert installed.stdout == f"{payload}\n"
    subprocess.run(["podman", "rmi", *images], capture_output=True, check=False)


def test_parsing_gets_the_pins_of_the_image_only(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a store of its own, and a build root with a pin the image does not have
    monkeypatch.setattr(pinned, "VOLUME", volume := f"xcraft-pinned-test-{os.getpid()}-{time.monotonic_ns()}")
    monkeypatch.setattr(pinned, "_checked", set())
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nBuildRequires: lzo-devel"))
    try:
        assert xcraft("build", "--dry-run")[0] == 0
        lock = load_lock(Project.find())
        pins = lock.binaries[ARCH]
        assert "lzo-devel" in pins
        # parsed again, once pinned
        shutil.rmtree(root / ".xcraft/cache/specs")
        assert xcraft("build", "--dry-run")[0] == 0
        stored = {path.name for path in (pinned._store() / "rpms").glob("*/*.rpm")}
        assert stored <= {pinned.file_name(name, pins[name]) for name in lock.image[ARCH]}
    finally:
        subprocess.run(["podman", "volume", "rm", "-f", volume], check=False, capture_output=True)


def test_pinned_store_checked(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # a store of its own
    monkeypatch.setattr(pinned, "VOLUME", volume := f"xcraft-pinned-test-{os.getpid()}-{time.monotonic_ns()}")
    monkeypatch.setattr(pinned, "_checked", set())
    project = Project.find()
    try:
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
    finally:
        subprocess.run(["podman", "volume", "rm", "-f", volume], check=False, capture_output=True)


def test_clean_removes_the_other_repositories_of_pinned_packages(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = Project.find()
    used = {pinned.repository(project), pinned.repository(project, container._image_pins(project))}
    assert {repo.name for repo in used} <= container.checkout_repositories(project)
    # a store of its own
    monkeypatch.setattr(pinned, "VOLUME", volume := f"xcraft-pinned-test-{os.getpid()}-{time.monotonic_ns()}")
    try:
        store = pinned._store()
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
    finally:
        subprocess.run(["podman", "volume", "rm", "-f", volume], check=False, capture_output=True)


def test_signatures_of_the_keys_of_the_repositories(root: Path, tmp_path: Path) -> None:
    # signed with the key of AlmaLinux's EPEL, of a URL, which the build image does not have, as it installs the pinned
    # packages without the keys
    srpm = tmp_path / "htop-3.3.0-5.el10_0.alma_altarch.src.rpm"
    process.download(f"https://epel.vault.almalinux.org/10/Source/Packages/{srpm.name}", srpm)
    check_signatures(Project.find(), tmp_path, [srpm.name], what="upstream source packages")


def test_building_until_recorded(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert True in states
    assert not builder.building(Project.find(), "trivial")


def test_status_during_a_build(root: Path) -> None:
    assert xcraft("build", "--dry-run")[0] == 0
    with (root / ".xcraft/lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # as a build does
        code, output = xcraft("status")
        assert code == 0, output


def test_patchset_during_a_build(root: Path, tmp_path: Path) -> None:
    repo = tmp_path / "trivial"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.com"]
    for args in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "upstream"], ["tag", "1.0"]):
        subprocess.run([*git, *args], check=True)
    (repo / "fix").write_text("fix\n")
    subprocess.run([*git, "add", "fix"], check=True)
    subprocess.run([*git, "commit", "-qm", "a fix"], check=True)
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace("%prep\n", "%prep\n%autopatch -p1\n"))
    with (root / ".xcraft/lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)  # as a build does
        # the package of the repository by its name, and its version from its spec only, without the lock
        code, output = xcraft("patchset", str(repo))
        assert code == 0, output
    assert output.startswith("trivial: patchset from 1.0, 1 added")
    assert (root / "base/trivial/0001-a-fix.patch").exists()
    assert xcraft("patchset", str(repo), "--package", "nope")[0] == 2


def test_ccache_with_a_compiler(root: Path) -> None:
    write_spec(root, "compiled", "gcc")
    assert xcraft("build", "--dry-run")[0] == 0
    roots = load_lock(Project.find()).roots.shared
    assert {"gcc", "ccache"} <= set(roots["compiled"])
    assert "ccache" not in roots.get("trivial", [])


def test_lock_records_image_and_roots(root: Path) -> None:
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
    # recorded again when they change, which --locked forbids
    spec.write_text(original)
    code, output = xcraft("build", "--dry-run", "--locked")
    assert code == 1
    assert "--locked forbids: " in output
    assert "-roots.trivial lzo" in output
    assert xcraft("build", "--dry-run")[0] == 0
    assert "lzo" not in load_lock(Project.find()).roots.shared.get("trivial", [])
    # nor kept for packages removed from the project
    gone = 'gone.requires."gone" = ["bash"]\ngone.aarch64.provides."gone" = ["zsh"]\ngone.buildrequires = ["bash"]\n'
    with builds_path(Project.find()).open("a") as f:
        f.write(f"\n# gone\n{gone}")
    assert xcraft("build", "--dry-run")[0] == 0
    assert "gone" not in builds_path(Project.find()).read_text()
