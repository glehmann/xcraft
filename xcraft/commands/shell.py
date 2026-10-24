"""xcraft shell."""

from __future__ import annotations

from xcraft import rpmbuild
from xcraft.commands import Arch, OnePackage, load


def shell(package: OnePackage, arch: Arch = None) -> None:
    """Open a shell in the build tree of a package, where rpmbuild -ba SPECS/<spec> builds it, without the network.

    Its build dependencies are installed, and its sources prepared. The build tree is .xcraft/build/<package>/ on the
    host, where its files can be edited, and its packages are.
    """
    project = load([package], arch=arch)
    with project.lock():
        rpmbuild.shell(project, package)
