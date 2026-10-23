"""The fixtures of all the tests."""

from __future__ import annotations

import fcntl
import shutil

import pytest

from tests import PROJECT, config
from xcraft.container import build_image
from xcraft.project import Project


@pytest.fixture(scope="session", autouse=True)
def build_environment(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Build the image of the build environment of the test projects first, once for all the workers of pytest-xdist,
    rather than all of them at once.
    """  # noqa: D205
    if not shutil.which("podman"):
        return
    with (tmp_path_factory.getbasetemp().parent / "build-environment.lock").open("w") as f:  # of all the workers
        fcntl.flock(f, fcntl.LOCK_EX)
        root = tmp_path_factory.mktemp("build-environment") / "project"
        shutil.copytree(PROJECT, root)
        project = Project(root, config())
        build_image(next((p for p in project.all_platforms() if p.native), project))
