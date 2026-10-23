"""The tests, with the project they copy: project/, its xcraft.toml and packages."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from xcraft.project import Config

PROJECT = Path(__file__).parent / "project"
TOML = (PROJECT / "xcraft.toml").read_text()
# the platform of the host, which the tests build for, natively, like x86_64_v2 on x86_64
PLATFORM, ARCH = {"x86_64": ("linux/amd64/v2", "x86_64_v2"), "aarch64": ("linux/arm64", "aarch64")}[os.uname().machine]


def config() -> Config:
    """Return the configuration of the test project."""
    return Config.model_validate(tomllib.loads(TOML))
