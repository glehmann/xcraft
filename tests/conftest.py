"""The fixtures of all the tests."""

from __future__ import annotations

import fcntl
import os
import shutil

import pytest

import tests
from tests import PROJECT, config
from xcraft.build import build_keys
from xcraft.container import build_image
from xcraft.project import Project


@pytest.fixture(scope="session")
def build_environment(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Build the image of the build environment of the test projects first, and resolve their lock, once for all the
    workers of pytest-xdist, rather than all of them at once: the tests in containers copy that template, the others
    do not need it.
    """  # noqa: D205
    if not shutil.which("podman"):
        return
    basetemp = tmp_path_factory.getbasetemp()
    shared = basetemp.parent if "PYTEST_XDIST_WORKER" in os.environ else basetemp  # of all the workers of the session
    with (shared / "build-environment.lock").open("w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        root = shared / "template"
        if not (root / "xcraft.lock").exists():
            shutil.rmtree(root, ignore_errors=True)
            shutil.copytree(PROJECT, root)
            native = Project(root, config()).on_host()
            build_image(native)
            build_keys(native)
    tests.TEMPLATE = root
