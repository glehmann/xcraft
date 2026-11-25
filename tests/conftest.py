"""The fixtures of all the tests."""

from __future__ import annotations

import contextlib
import fcntl
import functools
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests import ARCH, GARAGE, PROJECT, REMOTE, S3_PORT, TOML, TOYS, UPSTREAM, WEB_PORT, config, copy_project, garage
from xcraft import container, process, resolution, upstream_copy
from xcraft.cache import build_keys
from xcraft.container import BUILDENV, built_image, repo_file, run_natively
from xcraft.localrepo import extra_rpms
from xcraft.project import Project, write_atomically
from xcraft.resolution import Resolved
from xcraft.signing import Signer
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

os.environ["PYTHON_KEYRING_BACKEND"] = "keyring.backends.null.Keyring"  # never the developer's
os.environ["XCRAFT_BUILDERS"] = "x86_64_v2=,aarch64="  # nor its builders, see xcraft.builders.builders()


def _build_toys(root: Path) -> None:
    """Build the upstream packages of the tests into their repository, TOYS, unless it is there already, in the base
    image of the host, with the repositories of the project but theirs, in `root`.
    """
    if TOYS.exists():
        return
    config_ = config()
    del config_.repos["toys"]
    project = Project(root, config_).on_host()
    work = TOYS.with_name(f".{TOYS.name}-{os.getpid()}")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    run_natively(
        project,
        "toys",
        ["bash", "-e", "/specs/build.sh"],
        volumes=[f"{UPSTREAM}:/specs:ro", f"{work}:/out"],
        show="the toys",
    )
    (work / "repo").rename(TOYS)
    shutil.rmtree(work)
    for old in TOYS.parent.glob("[!.]*"):  # of older specs
        if old != TOYS:
            shutil.rmtree(old)


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # of another user
        pass
    return True


def pytest_sessionstart(session: pytest.Session) -> None:
    """Remove the volumes of the tests, xcraft-<name>-test-<pid>[-<id>], of the processes not running anymore, which
    ended without removing them, like killed ones.
    """
    if not shutil.which("podman"):
        return
    volumes = process.run(["podman", "volume", "ls", "--quiet"], capture=True).stdout.split()
    if ended := [
        v for v in volumes if (m := re.fullmatch(r"xcraft-.+-test-(\d+)(-.+)?", v)) and not _running(int(m[1]))
    ]:
        process.run(["podman", "volume", "rm", "--force", *ended], check=False, capture=True)


def _ended(run: Path, home: Path) -> bool:
    """Return whether the session of the temporary directory `run` of pytest ended without stopping the gpg agent of
    `home`: pytest removed it since, or the session was killed, leaving the lock naming its process.
    """
    lock = run / ".lock"
    with contextlib.suppress(OSError, ValueError):
        return not home.exists() or not _running(int(lock.read_text()))
    return not home.exists()


@pytest.fixture(scope="session", autouse=True)
def stop_gpg_agents(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Stop at the end the gpg agents of the keyrings of the tests, in the temporary directory of the session, which gpg
    starts and leaves running, and those of earlier sessions killed before stopping them, see _ended().
    """
    yield
    if not shutil.which("gpgconf"):
        return
    base = tmp_path_factory.getbasetemp()
    root = next(p for p in base.parents if p.name.startswith("pytest-of-"))
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = cmdline.read_bytes().decode(errors="replace").split("\0")
        except OSError:  # ended
            continue
        if Path(args[0]).name == "gpg-agent" and "--homedir" in args[:-1]:
            home = Path(args[args.index("--homedir") + 1])
            if home.is_relative_to(base):
                process.run(["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"], check=False, capture=True)
            elif home.is_relative_to(root) and _ended(root / home.relative_to(root).parts[0], home):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(int(cmdline.parent.name), signal.SIGTERM)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Mark the tests in the build environment as needing podman, and skip those needing it without it, or all of them
    on an architecture the test project does not build for; keep those of the shard $XCRAFT_TEST_SHARD only, like 1/2
    for every other test, for the jobs of CI to share them.
    """
    if shard := os.environ.get("XCRAFT_TEST_SHARD"):
        index, count = (int(n) for n in shard.split("/"))
        config.hook.pytest_deselected(items=[item for i, item in enumerate(items) if i % count != index - 1])
        items[:] = [item for i, item in enumerate(items) if i % count == index - 1]
    for item in items:
        if not ARCH:
            item.add_marker(pytest.mark.skip(reason=f"no platform of the tests for {os.uname().machine}"))
        if "build_environment" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.podman)
        if item.get_closest_marker("podman") and not shutil.which("podman"):
            item.add_marker(pytest.mark.skip(reason="needs podman"))


def _shared_resolutions(kept: Path, session: Path) -> Callable[..., Resolved]:
    """Return resolution._run(), resolving once for all the workers what the tests resolve the same, by what it is
    resolved from: the built RPMs too, by SHA-256, and the metadata of the local repositories, which some tests change.
    Those resolved from neither, but the toys, are `kept` for the next sessions; the others, which their signatures,
    timestamps and temporary paths make different each time, are for the `session` only. The original is its
    __wrapped__, for the tests of the resolutions.
    """
    run = resolution._run

    @functools.wraps(run)
    def shared(project: Project, data: dict, *, show: str, rpms: Sequence[Path] = (), quiet: bool = False) -> Resolved:
        local = [Path(volume.split(":")[0]) / "repodata/repomd.xml" for volume in container.local_repos(project)]
        inputs = [
            (BUILDENV / "resolve.py").read_text(), repo_file(project), upstream_copy.repo_file(project), project.arch,
            project.config.base_image, data, sorted(sha256_of(f) for f in [*rpms, *extra_rpms(project)]),
            [sha256_of(f) if f.exists() else "" for f in local],
        ]  # fmt: skip
        directory = session if rpms or not all(f.is_relative_to(TOYS) for f in local) else kept
        path = directory / f"{hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()}.json"
        if path.exists():
            os.utime(path)  # used, see build_environment()
            return Resolved.model_validate_json(path.read_text())
        resolved = run(project, data, show=show, rpms=rpms, quiet=quiet)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_atomically(path, resolved.model_dump_json())
        return resolved

    return shared


@pytest.fixture(scope="session")
def build_environment(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Build the image of the build environment of the test projects first, and resolve their lock, once for all the
    workers of pytest-xdist, rather than all of them at once, and return that prepared project: the tests in
    containers copy that template, the others do not need it.

    From then on, until the end of the session, the resolutions are shared by all the workers, and each worker has its
    own dnf caches.
    """
    basetemp = tmp_path_factory.getbasetemp()
    xdist = "PYTEST_XDIST_WORKER" in os.environ
    shared = basetemp.parent if xdist else basetemp  # of all the workers of the session
    # of all the sessions, those CI keeps the ones used of, and those of this one only
    kept = Path(os.environ.get("XCRAFT_TEST_RESOLUTIONS", Path(__file__).parent / "resolutions"))
    resolutions = _shared_resolutions(kept, shared / "resolutions")
    # Workers run real dnf transactions, with separate caches so their locks do not serialize the tests: in volumes,
    # with the others, rather than in the temporary files, often in memory, which the copies of all the workers fill
    cache_name = container._dnf_cache
    volumes: dict[str, str] = {}

    def worker_cache(project: Project) -> str:
        source = cache_name(project)
        if source not in volumes:
            volume = volumes[source] = f"{source}-test-{os.getpid()}"
            process.run(["podman", "volume", "create", volume], capture=True)
            process.run(
                ["podman", "run", "--rm", "--security-opt", "label=disable", "--volume", f"{source}:/from",
                 "--volume", f"{volume}:/to", container.base_image(project.on_host()), "flock", "--shared",
                 "/from/.xcraft.lock", "cp", "-a", "--reflink=auto", "/from/.", "/to"],
                capture=True,
            )  # fmt: skip
        return volumes[source]

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(resolution, "_run", resolutions)
        with (shared / "build-environment.lock").open("w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            _build_toys(shared)
            root = shared / "template"
            if not (root / "xcraft.lock").exists():
                for path in kept.glob("*.json"):  # unused for a month, like those of other versions of the tests
                    if path.stat().st_mtime < time.time() - 30 * 86400:
                        path.unlink(missing_ok=True)
                shutil.rmtree(root, ignore_errors=True)
                shutil.copytree(PROJECT, root)
                (root / "xcraft.toml").write_text(TOML)
                native = Project(root, config()).on_host()
                resolution.prepare(native)
                built_image(native)
                build_keys(native)
        try:
            if xdist:
                patch.setattr(container, "_dnf_cache", worker_cache)
                worker_cache(Project(root, config()).on_host())
            yield root
        finally:
            for volume in volumes.values():
                process.run(["podman", "volume", "rm", "-f", volume], check=False, capture=True)


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, build_environment: Path) -> Path:
    """Return a copy of the prepared test project, the project of xcraft: in the build environment, whose image is
    built the first time, which needs the network, see the podman marker.
    """
    root = tmp_path / "project"
    copy_project(root, build_environment)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


@pytest.fixture(scope="session")
def s3(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, str]]:
    """Start a Garage server with a bucket, a key to write it, and public reads; return its settings."""
    garage_config = tmp_path_factory.mktemp("garage") / "garage.toml"
    garage_config.write_text(
        'metadata_dir = "/var/lib/garage/meta"\ndata_dir = "/var/lib/garage/data"\ndb_engine = "sqlite"\n'
        f'replication_factor = 1\nrpc_bind_addr = "[::]:3901"\nrpc_secret = "{secrets.token_hex(32)}"\n'
        '[s3_api]\ns3_region = "garage"\napi_bind_addr = "[::]:3900"\nroot_domain = ".s3.garage.localhost"\n'
        '[s3_web]\nbind_addr = "[::]:3902"\nroot_domain = ".web.garage.localhost"\nindex = "index.html"\n'
    )
    subprocess.run(["podman", "rm", "-f", GARAGE], check=True, capture_output=True)
    subprocess.run(
        ["podman", "run", "-d", "--rm", "--name", GARAGE, "-p", f"127.0.0.1:{S3_PORT}:3900",
         "-p", f"127.0.0.1:{WEB_PORT}:3902", "-v", f"{garage_config}:/etc/garage.toml:ro,Z",
         "docker.io/dxflrs/garage:v2.1.0"],
        check=True, capture_output=True,
    )  # fmt: skip
    try:
        for _ in range(50):
            try:
                node = garage("node", "id", "-q").split("@")[0].strip()
                break
            except subprocess.CalledProcessError:
                time.sleep(0.2)
        else:
            pytest.fail("the Garage server did not start")
        garage("layout", "assign", "-z", "dc1", "-c", "1G", node)
        garage("layout", "apply", "--version", "1")
        garage("bucket", "create", "xcraft")
        key = garage("key", "create", "test")
        garage("bucket", "allow", "--read", "--write", "--owner", "xcraft", "--key", "test")
        garage("bucket", "website", "--allow", "xcraft")
        garage("bucket", "create", "xcraft-dev")
        garage("bucket", "allow", "--read", "--write", "xcraft-dev", "--key", "test")
        garage("bucket", "website", "--allow", "xcraft-dev")
        fields = dict(re.findall(r"^(Key ID|Secret key):\s+(\S+)$", key, re.MULTILINE))
        yield {"AWS_ACCESS_KEY_ID": fields["Key ID"], "AWS_SECRET_ACCESS_KEY": fields["Secret key"]}
    finally:
        subprocess.run(["podman", "rm", "-f", GARAGE], check=True, capture_output=True)


@pytest.fixture(scope="session")
def build_signer(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    """Return the fingerprint of the key of the builds of the remote storage, like CI's, and its keyring."""
    if not shutil.which("gpg"):
        pytest.skip("needs gpg")  # for the manifests of all the trees
    home = tmp_path_factory.mktemp("build-key")
    home.chmod(0o700)
    signer = Signer("", home)
    signer.gpg("--passphrase", "", "--quick-generate-key", "CI test", "rsa2048", "sign", "never")
    fingerprint = next(
        line.split(":")[9]
        for line in signer.gpg("--with-colons", "--list-secret-keys").splitlines()
        if line.startswith("fpr:")
    )
    return fingerprint, home


@pytest.fixture
def storage_root(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    build_environment: Path,
    s3: dict[str, str],
    build_signer: tuple[str, Path],
) -> Path:
    """Return a copy of the test project, see root(), with the remote storage, its builds signed by build_signer:
    with builds and snapshots of its own, not to find those of the other tests in the storage, which they share, in a
    stage of its own, which the REMOTE and STAGE of the module of the test have.
    """
    root = tmp_path / "project"
    copy_project(root, build_environment)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    spec = root / "base/trivial/trivial.spec"
    spec.write_text(f"# {secrets.token_hex(4)}\n{spec.read_text()}")
    monkeypatch.setattr(request.module, "STAGE", stage := f"test-{secrets.token_hex(4)}")
    remote = REMOTE.replace(
        "[remote.main]",
        f'[sign]\nkey = "{build_signer[0]}"\ngnupghome = "{build_signer[1]}"\n'
        f'[remote.main]\nbuild_keys = ["{build_signer[0]}"]',
    ).replace("[publish]\n", f'[publish]\nstage = "{stage}"\n')
    monkeypatch.setattr(request.module, "REMOTE", remote)
    (root / "xcraft.toml").write_text(remote)
    monkeypatch.setenv("GNUPGHOME", str(tmp_path / "unused-keyring"))
    for name, value in s3.items():
        monkeypatch.setenv(name, value)
    return root


@pytest.fixture
def unprepared_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a copy of the test project, without its lock nor what xcraft resolved for it: for the tests failing or
    succeeding before any preparation, which need no build environment.
    """
    root = tmp_path / "project"
    copy_project(root)
    monkeypatch.setenv("XCRAFT_PROJECT", str(root))
    return root


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
    case "$arg" in
        h:/*) set -- "$@" "${arg#h:}";;
        h:*) set -- "$@" "$BUILDER_HOME/${arg#h:}";;
        *) set -- "$@" "$arg";;
    esac
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
