"""The packages of the project, as their specs define them, see xcraft.specs, and what their names say."""

from __future__ import annotations

from pydantic import BaseModel


def dep_name(dep: str) -> str:
    """Return the capability of a dependency, like ``foo`` for ``foo >= 1.0``, or a rich one whole."""
    return dep if dep.startswith("(") else dep.split(" ", 1)[0]


def is_debug(name: str) -> bool:
    """Return whether `name` is a debug package, which rpm splits from the others while building."""
    return name.endswith(("-debuginfo", "-debugsource"))


class Binary(BaseModel):
    """A binary package defined by a spec."""

    name: str
    evr: str
    arch: str
    provides: list[str]
    """Its capabilities, with their versions, like ``foo = 1:2-3``."""
    requires: list[str]
    recommends: list[str] = []
    autorev_name: str = ""
    """Its name with ``%{autorev}`` where the release suffix of the builds goes, for those named after their release,
    like the livepatch packages of the kernel."""


class Package(BaseModel):
    """What xcraft knows about a package directory, from its spec."""

    name: str = ""
    version: str = ""
    binaries: list[Binary] = []
    buildrequires: list[str] = []
    dist: str = ""
    """The value of %{dist}, at the end of the release of the binaries."""
    source0: str = ""
    """Source0, expanded: the URL of its archive, if the spec gives it."""
    sources: list[str] = []
    """All the sources, expanded, Source0 first: the URLs of their archives, for those the spec gives."""
    error: str = ""
    """Why the spec could not be parsed, if it could not."""

    @property
    def requirements(self) -> list[str]:
        """Its BuildRequires, and the requirements of its binary packages."""
        return [*self.buildrequires, *(r for b in self.binaries for r in b.requires)]

    def suffixed(self, text: str, evr: str, suffix: str) -> str:
        """Return `text`, of a binary package of release, or epoch-version-release, `evr`, as built with the release
        `suffix`, %autorev: at the start of %dist, wherever the spec has it, or else after the release.
        """
        if self.dist and self.dist in evr:
            return text.replace(self.dist, f"{suffix}{self.dist}")
        return text.replace(evr, f"{evr}{suffix}") if suffix else text

    def built_names(self, suffix: str) -> list[str]:
        """Return the names of its binary packages, as built with the release `suffix`."""
        return [b.autorev_name.replace("%{autorev}", suffix) if b.autorev_name else b.name for b in self.binaries]
