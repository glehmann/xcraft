"""Builds in the real build environment, on the fixture packages."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from xcraft import container
from xcraft.__main__ import app
from xcraft.lock import Lock, load_lock, save_lock
from xcraft.project import Project
from xcraft.sources import write_manifest

FIXTURES = Path(__file__).parent / "fixtures"
runner = CliRunner()


# the build environment image is built the first time, which needs the network
pytestmark = pytest.mark.skipif(not shutil.which("podman"), reason="needs podman")


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    (root / "specs/base").mkdir(parents=True)
    for name in ("trivial", "with-deps"):
        shutil.copytree(FIXTURES / name, root / "specs/base" / name)
    (root / "xcraft.toml").write_text("")
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


def results(output: str) -> list[str]:
    """Return the lines of the results of a build, without its errors and warnings."""
    return [line for line in output.splitlines() if line.startswith(("✓", "·", "✗", "-", "↓")) or line[:1].isdigit()]


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
    assert output.splitlines() == ["· trivial cached", "· with-deps cached", "2 cached"]
    assert list((root / ".xcraft/cache/builds/x86_64_v2/with-deps").glob("*/RPMS/noarch/with-deps-1.0-*.noarch.rpm"))

    # a change in trivial changes the key of with-deps too
    spec = root / "specs/base/trivial/trivial.spec"
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
    code, output = xcraft("build", "--test", "fix1")
    assert code == 2
    assert "test and pre builds are of the given packages only" in output


def test_spec_changed_during_build_is_not_cached(root: Path) -> None:
    spec = root / "specs/base/trivial/trivial.spec"

    def change() -> None:
        while not (root / ".xcraft/build/trivial.pid").exists():
            time.sleep(0.1)
        spec.write_text(f"{spec.read_text()}# changed\n")

    threading.Thread(target=change, daemon=True).start()
    code, output = xcraft("build", "trivial")
    assert code == 1
    assert "specs/base/trivial changed during the build, build it again" in output
    assert states() == {"trivial": "missing", "with-deps": "missing"}


def write_spec(root: Path, name: str, requires: str = "", files: str = "/usr/share/doc/%{name}") -> None:
    """Write a minimal noarch package installing `files`, with the BuildRequires `requires`."""
    (root / "specs/base" / name).mkdir()
    (root / "specs/base" / name / f"{name}.spec").write_text(
        f"Name: {name}\nVersion: 1.0\nRelease: 1\nSummary: test\nLicense: MIT\nBuildArch: noarch\n"
        + (f"BuildRequires: {requires}\n" if requires else "")
        + f"%description\ntest\n%install\nmkdir -p %{{buildroot}}{files.rsplit('/', 1)[0]}\n"
        + f"touch %{{buildroot}}{files}\n%files\n{files}\n"
    )


def test_build_root_unexplained_by_dependencies(root: Path) -> None:
    # xcraft does not know which files our packages provide before they are built
    write_spec(root, "tool", files="/usr/bin/xcraft-test-tool")
    write_spec(root, "tool-user", "/usr/bin/xcraft-test-tool")
    assert xcraft("build", "tool")[0] == 0
    code, output = xcraft("build", "tool-user")
    assert code == 1
    assert "installed our tool (tool), missing from its dependencies" in output
    # upstream's bc, instead of ours, never built since nothing is known to need it
    write_spec(root, "bc")
    write_spec(root, "bc-user", "/usr/bin/bc")
    code, output = xcraft("build", "bc-user")
    assert code == 1
    assert "installed the upstream bc (ours from bc)" in output


def test_image_packages_replaced_by_ours(root: Path) -> None:
    # R-srpm-macros is in the image, and harmless to replace
    write_spec(root, "R-srpm-macros", files="/usr/share/xcraft-test/R-srpm-macros")
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert [line.split(" built")[0] for line in results(output)[:2]] == ["✓ R-srpm-macros", "✓ trivial"]
    assert re.search(
        r"^Downgrading:\n R-srpm-macros .* xcraft-[0-9a-f]{8} ",
        root.joinpath(".xcraft/logs/trivial.log").read_text(),
        re.MULTILINE,
    )


def test_test_and_pre_builds(root: Path) -> None:
    def rpms(*args: str) -> list[str]:
        code, output = xcraft("build", *args)
        assert code == 0, output
        return [Path(line).name for line in output.splitlines() if line.endswith(".rpm")]

    assert rpms("trivial", "--test", "fix1") == ["trivial-1.0-1.0.fix1.1.xcpng8.99.noarch.rpm"]
    assert rpms("trivial", "--test", "fix1") == ["trivial-1.0-1.0.fix1.2.xcpng8.99.noarch.rpm"]
    assert rpms("trivial", "--pre", "fix1") == ["trivial-1.0-1~fix1.1.xcpng8.99.noarch.rpm"]
    # the test builds are not the normal one, and do not change its build root
    assert states() == {"trivial": "missing", "with-deps": "missing"}
    assert "trivial" not in load_lock(Project.find()).buildroots
    assert xcraft("build", "trivial", "--test", "fix-1")[0] == 2

    def tests() -> str:
        return " ".join(xcraft("status", "trivial")[1].splitlines()[1].split()[3:])

    # the latest of each ID, the newest first
    assert tests() == "fix1.1 (pre, just now), fix1.2 (test, just now)"
    spec = root / "specs/base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "hello again"'))
    assert tests() == "fix1.1 (pre, just now, outdated), fix1.2 (test, just now, outdated)"


def test_repo_of_test_builds(root: Path) -> None:
    code, output = xcraft("repo", "with-deps", "--test", "fix1", "--baseurl", "http://example.com/fix1/")
    assert code == 0, output
    repo = root / "repo-fix1"
    # only the test build, not trivial, which the hosts have
    assert sorted(p.name for p in repo.glob("*.rpm")) == ["with-deps-1.0-1.0.fix1.1.xcpng8.99.noarch.rpm"]
    assert (repo / "repodata/repomd.xml").exists()
    assert "baseurl=http://example.com/fix1/\n" in (repo / "xcraft-fix1.repo").read_text()
    assert output.splitlines()[-2:] == [
        "on the hosts, with this directory at http://example.com/fix1/:",
        "  copy xcraft-fix1.repo to /etc/yum.repos.d/, then: dnf upgrade --refresh with-deps",
    ]


def test_repo_host_options(root: Path) -> None:
    for args, error in [
        (["--host", "h"], "--host and --upgrade are for test builds"),
        (["--test", "a", "--upgrade"], "--upgrade needs --host"),
        (["--test", "a", "--host", "h", "--baseurl", "http://x/"], "with --host, the hosts find the repository in"),
    ]:
        code, output = xcraft("repo", "trivial", *args)
        assert code == 2
        assert error in " ".join(output.replace("│", "").split())


def test_dnf_lock_is_shared_by_containers(root: Path) -> None:
    logs = [root / f"lock-{i}.log" for i in range(2)]
    container.build_image(Project.find())  # before the threads, which would both build it

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
    # a build root its build does not get: missing, and built again, back to the key of the first build
    lock = load_lock(Project.find())
    lock.buildroots["trivial"] = ["bash"]
    save_lock(Project.find(), lock)
    code, output = xcraft("build", "trivial")
    assert code == 0, output
    assert results(output) == [results(output)[0], "1 built"]
    assert states() == {"trivial": "built", "with-deps": "missing"}
    assert not list((root / ".xcraft/cache/builds/x86_64_v2/trivial").glob(".*"))


def test_failed_build_skips_dependents(root: Path) -> None:
    spec = root / "specs/base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello"', 'echo "build failure"; exit 3'))
    code, output = xcraft("build", "with-deps")
    assert code == 1
    assert "✗ trivial: failed, see " in output
    assert "echo 'build failure'" in output  # the failed rpmbuild step is shown
    assert "- with-deps skipped, needs trivial" in output
    assert output.splitlines()[-1] == "1 failed, 1 skipped"
    code, output = xcraft("log", "trivial", "--step", "build", "--grep", "failure")
    assert code == 0
    assert "build failure" in output


def test_missing_archive_and_spec_error(root: Path) -> None:
    write_manifest(root / "specs/base/trivial/sources", {"trivial-1.0.tar.gz": "0" * 64})
    (root / "specs/base/broken").mkdir()
    (root / "specs/base/broken/broken.spec").write_text("Name: broken\n%if\n")
    code, output = xcraft("build", "trivial", "broken")
    assert code == 1
    assert "trivial-1.0.tar.gz is not in the source cache, run: xcraft import trivial" in output
    assert "✗ broken: " in output
    assert re.search(r"^broken +error *$", xcraft("status")[1], re.MULTILINE)
    assert xcraft("st") == xcraft("status")


def test_repo_checks_dependencies(root: Path) -> None:
    code, output = xcraft("repo", "with-deps")
    assert code == 0, output
    assert f"repository in {root / 'repo'}, with all dependencies satisfied" in output
    assert sorted(p.name.split("-1.0")[0] for p in (root / "repo").glob("*.rpm")) == ["trivial", "with-deps"]
    assert (root / "repo/repodata/repomd.xml").exists()
    # with the cached metadata and builds, after clean removed .xcraft/tmp
    assert xcraft("clean")[0] == 0
    code, output = xcraft("repo", "with-deps")
    assert code == 0, output

    spec = root / "specs/base/trivial/trivial.spec"
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
    spec = (root / "specs/base/trivial/trivial.spec").read_text()
    assert "Release: 2%{?dist}\n" in spec
    assert "%changelog\n* " in spec
    assert " Me <me@example.com> - 1.0-2\n- Rebuild\n\n* Mon Jul 10 2026 Test User" in spec
    # without a message, an empty item to fill in, which rpm accepts
    code, output = xcraft("bump", "trivial")
    assert code == 0, output
    assert " Me <me@example.com> - 1.0-3\n-\n\n" in (root / "specs/base/trivial/trivial.spec").read_text()
    assert xcraft("build", "trivial")[0] == 0
    # with --dep, from the pending updates of all the platforms, cleared on all of them
    arm = Project.find("aarch64")
    save_lock(arm, Lock(pending={"trivial": {"zlib": "1.2-1"}}))
    assert xcraft("bump", "--dep", "zlib")[1] == "trivial: 1.0-3 -> 1.0-4\n"
    assert load_lock(arm).pending == {}


def test_bump_version(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gitconfig = root / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Me\n\temail = me@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    archives = root / "archives"
    archives.mkdir()
    (archives / "trivial-2.0.tar.gz").write_bytes(b"new")
    spec = root / "specs/base/trivial/trivial.spec"
    source = f"Source0: file://{archives}/trivial-%{{version}}.tar.gz\n"
    spec.write_text(spec.read_text().replace("License: MIT\n", f"License: MIT\n{source}"))
    (root / "specs/base/trivial/sources").write_text(f"{'0' * 64}  trivial-1.0.tar.gz\n")
    # another package of the same archive, through a link
    twin = root / "specs/base/twin"
    twin.mkdir()
    (twin / "twin.spec").write_text(spec.read_text().replace("Name: trivial", "Name: twin"))
    (twin / "sources").symlink_to("../trivial/sources")
    (root / "xcraft.toml").write_text('same_version = [["trivial", "twin"]]\n')
    code, output = xcraft("bump", "twin", "--version", "2.0")
    assert code == 0, output
    assert "trivial: 1.0-1 -> 2.0-1\ntwin: 1.0-1 -> 2.0-1\n" in output
    assert "Version: 2.0\nRelease: 1%{?dist}\n" in spec.read_text()
    assert " Me <me@example.com> - 2.0-1\n- Update to 2.0\n" in (twin / "twin.spec").read_text()
    digest = hashlib.sha256(b"new").hexdigest()
    assert (root / "specs/base/trivial/sources").read_text() == f"{digest}  trivial-2.0.tar.gz\n"
    assert (twin / "sources").is_symlink()
    assert (root / ".xcraft/cache/sources" / digest / "trivial-2.0.tar.gz").read_bytes() == b"new"
    # without a URL to download the archive from, nothing changes
    before = (root / "specs/base/with-deps/with-deps.spec").read_text()
    code, output = xcraft("bump", "with-deps", "--version", "2.0", "-m", "Update")
    assert code == 1
    assert "the Source0 of with-deps is not a URL" in output
    assert (root / "specs/base/with-deps/with-deps.spec").read_text() == before


def test_runaway_log_stops_the_build(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(container, "MAX_LOG_SIZE", 2**20)
    spec = root / "specs/base/trivial/trivial.spec"
    spec.write_text(spec.read_text().replace('echo "hello" > hello.txt', "yes looping"))
    code, output = xcraft("build", "trivial")
    assert code == 1
    assert "✗ trivial: stopped, its log got larger than 1 MiB" in output
    name = f"xcraft-{Project.find().id}-trivial"
    assert subprocess.run(["podman", "container", "exists", name], check=False).returncode != 0


def test_lock(root: Path) -> None:
    assert xcraft("build", "trivial")[0] == 0
    lock = load_lock(Project.find())
    assert "rpm-build" in lock.image
    assert lock.buildroots == {"trivial": []}

    spec = root / "specs/base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nBuildRequires: zlib-ng-compat-devel")
    )
    code, output = xcraft("build", "trivial", "--locked")
    assert code == 1
    assert "the lock would change, which --locked forbids: " in output
    assert "+buildroots.trivial zlib-ng-compat-devel, " in output
    assert "zlib-ng-compat-devel" not in load_lock(Project.find()).packages

    assert xcraft("build", "trivial")[0] == 0
    lock = load_lock(Project.find())
    assert lock.packages["zlib-ng-compat-devel"].source == "zlib-ng"
    assert "zlib-ng-compat-devel" in lock.buildroots["trivial"]
    assert xcraft("build", "trivial", "--locked") == (0, "· trivial cached\n1 cached\n")
    assert xcraft("lock", "--update", "zlib-ng")[1].endswith("updated\n")

    # a build root of locked packages, but not the one of the lock
    spec.write_text(spec.read_text().replace("BuildRequires: zlib-ng-compat-devel\n", ""))
    code, output = xcraft("build", "trivial", "--locked")
    assert code == 1
    assert "-buildroots.trivial zlib-ng-compat-devel" in output
    assert "zlib-ng-compat-devel" in load_lock(Project.find()).buildroots["trivial"]


def test_doctor(root: Path) -> None:
    code, output = xcraft("doctor")
    assert code == 0, output
    assert output.startswith("✓ podman ")
    assert "✓ git: " in output


def test_graph(root: Path) -> None:
    assert xcraft("graph", "with-deps") == (0, "with-deps\n└── trivial\n")
    assert xcraft("graph", "with-deps", "--flat") == (0, "trivial\nwith-deps\n")
    assert xcraft("graph", "trivial", "--dependents") == (0, "trivial\n└── with-deps\n")
    assert xcraft("graph", "trivial", "--dependents", "--flat") == (0, "with-deps\n")


def test_impacts_and_bump(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = root / "specs/base/trivial/trivial.spec"
    spec.write_text(
        spec.read_text().replace("BuildArch: noarch", "BuildArch: noarch\nBuildRequires: zlib-ng-compat-devel")
    )
    assert xcraft("build", "with-deps")[0] == 0
    assert xcraft("impacts") == (0, "nothing pending\n")
    # through trivial, with-deps is built against the upstream package
    assert xcraft("graph", "zlib-ng-compat-devel", "--dependents", "--flat") == (0, "trivial\nwith-deps\n")
    assert "└── zlib-ng-compat-devel\n" in xcraft("graph", "trivial", "--upstream")[1]

    # as if zlib-ng-compat-devel had been updated since trivial was built
    lock = load_lock(Project.find())
    lock.note_updates({"zlib-ng-compat-devel": "0:1.0-1"})
    save_lock(Project.find(), lock)
    code, output = xcraft("impacts")
    assert code == 0, output
    assert "zlib-ng-compat-devel  0:1.0-1" in output
    assert "✓ trivial" in output  # built with the current lock

    gitconfig = root / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Me\n\temail = me@example.com\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    assert xcraft("bump", "--dep", "zlib-ng-compat-devel", "-m", "Rebuild for zlib-ng") == (
        0,
        "trivial: 1.0-1 -> 1.0-2\n",
    )
    assert load_lock(Project.find()).pending == {}
