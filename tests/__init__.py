"""The tests, with the project they copy: project/, its xcraft.toml and packages."""

from __future__ import annotations

import hashlib
import os
import shutil
import tomllib
from pathlib import Path

from xcraft.project import Config

PROJECT = Path(__file__).parent / "project"
UPSTREAM = Path(__file__).parent / "upstream"
TOYS = (
    UPSTREAM / "repos" / hashlib.sha256(b"".join(f.read_bytes() for f in sorted(UPSTREAM.glob("*.*")))).hexdigest()[:16]
)
"""The repository of the upstream packages of the tests, by architecture, made by build.sh from their specs: see
conftest.py."""
TOML = (PROJECT / "xcraft.toml").read_text().replace("@TOYS@", str(TOYS))
# the platform of the host, which the tests build for, natively, like x86_64_v2 on x86_64
PLATFORM, ARCH = {"x86_64": ("linux/amd64/v2", "x86_64_v2"), "aarch64": ("linux/arm64", "aarch64")}[os.uname().machine]
TEMPLATE: Path | None = None
"""The project, with its lock and what xcraft resolved for it, made once for all the tests: see conftest.py."""


def config() -> Config:
    """Return the configuration of the test project."""
    return Config.model_validate(tomllib.loads(TOML))


def copy_project(dest: Path) -> None:
    """Copy the project to `dest`, with the lock and the resolutions of the template, when there is one."""

    def ignored(directory: str, names: list[str]) -> list[str]:
        kept = {".xcraft": ["cache"], ".xcraft/cache": ["base", "resolved", "specs"]}.get(
            Path(directory).relative_to(TEMPLATE or PROJECT).as_posix()
        )
        return [name for name in names if kept is not None and name not in kept]

    shutil.copytree(TEMPLATE or PROJECT, dest, ignore=ignored)
    (dest / "xcraft.toml").write_text(TOML)
