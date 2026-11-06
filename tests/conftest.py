"""The fixtures of all the tests."""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests import ARCH, PROJECT, TOML, TOYS, UPSTREAM, config
from xcraft import container, process, resolution, upstream_copy
from xcraft.cache import build_keys
from xcraft.container import BUILDENV, built_image, repo_file, run_natively
from xcraft.localrepo import extra_rpms
from xcraft.project import Project, write_atomically
from xcraft.resolution import Resolved
from xcraft.sources import sha256_of

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence


def make_toys(root: Path) -> None:
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


def _shared_resolutions(directory: Path) -> Callable[..., Resolved]:
    """Return resolution._run(), resolving once for all the workers what the tests resolve the same, by what it is
    resolved from: the built RPMs too, by SHA-256, and the metadata of the local repositories, which some tests change.
    The original is its __wrapped__, for the tests of the resolutions.
    """
    run = resolution._run

    @functools.wraps(run)
    def shared(project: Project, data: dict, *, show: str, rpms: Sequence[Path] = (), quiet: bool = False) -> Resolved:
        local = [Path(volume.split(":")[0]) / "repodata/repomd.xml" for volume in container._local_repos(project)]
        inputs = [
            (BUILDENV / "resolve.py").read_text(), repo_file(project), upstream_copy.repo_file(project), project.arch,
            project.config.base_image, data, sorted(sha256_of(f) for f in [*rpms, *extra_rpms(project)]),
            [sha256_of(f) if f.exists() else "" for f in local],
        ]  # fmt: skip
        path = directory / f"{hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()}.json"
        if path.exists():
            os.utime(path)  # used, see XCRAFT_TEST_RESOLUTIONS
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
    """
    basetemp = tmp_path_factory.getbasetemp()
    shared = basetemp.parent if "PYTEST_XDIST_WORKER" in os.environ else basetemp  # of all the workers of the session
    # of all the sessions, like those of CI, which keeps the ones used
    resolutions = Path(os.environ.get("XCRAFT_TEST_RESOLUTIONS", shared / "resolutions"))
    with (shared / "build-environment.lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        make_toys(shared)
        root = shared / "template"
        if not (root / "xcraft.lock").exists():
            shutil.rmtree(root, ignore_errors=True)
            shutil.copytree(PROJECT, root)
            (root / "xcraft.toml").write_text(TOML)
            native = Project(root, config()).on_host()
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(resolution, "_run", _shared_resolutions(resolutions))
                resolution.prepare(native)
                built_image(native)
                build_keys(native)
    if "PYTEST_XDIST_WORKER" not in os.environ:
        yield root
        return
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

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(container, "_dnf_cache", worker_cache)
            patch.setattr(resolution, "_run", _shared_resolutions(resolutions))
            worker_cache(Project(root, config()).on_host())
            yield root
    finally:
        for volume in volumes.values():
            process.run(["podman", "volume", "rm", "-f", volume], check=False, capture=True)
