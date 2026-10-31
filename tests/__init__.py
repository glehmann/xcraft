"""The tests, with the project they copy: project/, its xcraft.toml and packages."""

from __future__ import annotations

import hashlib
import os
import shutil
import tomllib
from pathlib import Path

import pytest

from xcraft.project import Config, Project
from xcraft.resolution import Resolved
from xcraft.specs import Binary, Package

PROJECT = Path(__file__).parent / "project"
UPSTREAM = Path(__file__).parent / "upstream"
TOYS = (
    UPSTREAM / "repos" / hashlib.sha256(b"".join(f.read_bytes() for f in sorted(UPSTREAM.glob("*.*")))).hexdigest()[:16]
)
"""The repository of the upstream packages of the tests, by architecture, made by build.sh from their specs: see
conftest.py."""
TOML = (PROJECT / "xcraft.toml").read_text().replace("@TOYS@", str(TOYS))
# the platform of the host, which the tests build for, natively, like x86_64_v2 on x86_64: none on the others, whose
# tests are skipped, see conftest.py
PLATFORM, ARCH = {"x86_64": ("linux/amd64/v2", "x86_64_v2"), "aarch64": ("linux/arm64", "aarch64")}.get(
    os.uname().machine, ("", "")
)
needs_gpg = pytest.mark.skipif(not shutil.which("gpg"), reason="needs gpg")
needs_git_lfs = pytest.mark.skipif(not shutil.which("git-lfs"), reason="needs git-lfs")


def config() -> Config:
    """Return the configuration of the test project."""
    return Config.model_validate(tomllib.loads(TOML))


def copy_project(dest: Path, template: Path = PROJECT) -> None:
    """Copy the project to `dest`: the `template` of the build_environment fixture has its lock and what xcraft
    resolved for it.
    """

    def ignored(directory: str, names: list[str]) -> list[str]:
        kept = {".xcraft": ["cache"], ".xcraft/cache": ["base", "resolved", "specs"]}.get(
            Path(directory).relative_to(template).as_posix()
        )
        return [name for name in names if kept is not None and name not in kept]

    shutil.copytree(template, dest, ignore=ignored)
    (dest / "xcraft.toml").write_text(TOML)


def ours(project: Project, monkeypatch: pytest.MonkeyPatch, deps: dict[str, set[str]]) -> None:
    """Give `project` packages of ours, as parsed, built against `deps`: each one with a binary package of its name,
    which those built against it require, in an image without packages of ours.
    """
    project.parsed = {
        name: Package(
            name=name,
            buildrequires=sorted(needed),
            binaries=[Binary(name=name, evr="1-1", arch="noarch", provides=[], requires=[])],
        )
        for name, needed in deps.items()
    }
    project.refresh()
    monkeypatch.setattr("xcraft.resolution.resolve_image", lambda _: Resolved())
