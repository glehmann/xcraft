"""xcraft shell."""

from __future__ import annotations

from xcraft import build as builder
from xcraft.commands import OnePackage, load


def shell(package: OnePackage) -> None:
    """Open a shell in the build environment of a package, with its sources prepared."""
    project = load([package])
    with project.lock():
        builder.shell(project, package)
