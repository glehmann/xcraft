"""The fixtures of all the tests."""

from __future__ import annotations

import fcntl
import os
import shutil
from typing import TYPE_CHECKING

import pytest

import tests
from tests import PROJECT, TOML, TOYS, UPSTREAM, config
from xcraft.cache import build_keys
from xcraft.container import build_image, run_natively
from xcraft.project import Project
from xcraft.resolution import prepare

if TYPE_CHECKING:
    from pathlib import Path


def make_toys(root: Path) -> None:
    """Build the upstream packages of the tests into their repository, TOYS, unless it is there already, in the base
    image of the host, with the repositories of the project but theirs, in `root`.
    """  # noqa: D205
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


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark the tests in the build environment as needing podman, and skip those needing it without it."""
    for item in items:
        if "build_environment" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.podman)
        if item.get_closest_marker("podman") and not shutil.which("podman"):
            item.add_marker(pytest.mark.skip(reason="needs podman"))


@pytest.fixture(scope="session")
def build_environment(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Build the image of the build environment of the test projects first, and resolve their lock, once for all the
    workers of pytest-xdist, rather than all of them at once: the tests in containers copy that template, the others
    do not need it.
    """  # noqa: D205
    basetemp = tmp_path_factory.getbasetemp()
    shared = basetemp.parent if "PYTEST_XDIST_WORKER" in os.environ else basetemp  # of all the workers of the session
    with (shared / "build-environment.lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        make_toys(shared)
        root = shared / "template"
        if not (root / "xcraft.lock").exists():
            shutil.rmtree(root, ignore_errors=True)
            shutil.copytree(PROJECT, root)
            (root / "xcraft.toml").write_text(TOML)
            native = Project(root, config()).on_host()
            prepare(native)
            build_image(native)
            build_keys(native)
    tests.TEMPLATE = root
