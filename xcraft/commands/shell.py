"""xcraft shell."""

from __future__ import annotations

from xcraft import build as builder
from xcraft.commands import Arch, OnePackage, load


def shell(package: OnePackage, arch: Arch = None) -> None:
    """Open a shell in the build environment of a package, with its sources prepared."""
    project = load([package], arch=arch)
    with project.lock():
        builder.shell(project, package)
