"""The tests, with the project they copy: project/, its xcraft.toml and packages."""

from __future__ import annotations

import tomllib
from pathlib import Path

from xcraft.project import Config

PROJECT = Path(__file__).parent / "project"
TOML = (PROJECT / "xcraft.toml").read_text()


def config() -> Config:
    """Return the configuration of the test project."""
    return Config.model_validate(tomllib.loads(TOML))
