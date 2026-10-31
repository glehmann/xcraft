"""Package metadata, extracted from the specs by rpm in the build environment.

All the specs are parsed in a single container, so that macros are expanded
and conditionals evaluated as in a build: natively, in the build environment of
the host, for the target architecture, like rpmbuild --target. The result is
cached until a package directory or the build environment image changes.
"""

from __future__ import annotations

import hashlib
import json
import stat
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter

from xcraft.container import BUILDENV, build_image, image_tag, run

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xcraft.project import Project


_SCRIPTS = [(BUILDENV / script).read_text() for script in ("parse.py", "rpmdeps.py")]
"""The parser of the specs, in the cache key of what it parsed."""


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


_Packages = TypeAdapter(dict[str, Package])


def main_spec(pkg_dir: Path) -> Path:
    """Return the spec of a package: ``<name>.spec``, or its only spec.

    Other specs are sources of the main one.
    """
    if (spec := pkg_dir / f"{pkg_dir.name}.spec").exists():
        return spec
    if len(specs := list(pkg_dir.glob("*.spec"))) == 1:
        return specs[0]
    msg = f"{pkg_dir.name} has no {pkg_dir.name}.spec" + (", and several other specs" if specs else "")
    raise RuntimeError(msg)


STAGED_TIME = 315532800
"""The modification time of what the build trees get, the same for all: 1980-01-01, the earliest zip archives have."""


def hash_dir(path: Path, root: Path | None = None, stage: Callable[[str, bytes, bool], None] | None = None) -> str:
    """Return a hash of the files in `path`, but its ``origin``, giving each one to `stage`, if any, by path, with its
    content and whether it is executable: what the build trees get, from the same reads, see rpmbuild.prepare_tree().

    That is their paths, contents, and whether their owners may run them, like git: the rest of their modes depends on
    the umask, and what the user may run on the mounts, with the ACLs. A symbolic link is the file it points to, in
    `root`, `path` by default, rather than out of the project, a directory, or nothing. The empty directories are not
    in it, nor in git.
    """
    h = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*") if p.is_symlink() or (p.is_file() and p != path / "origin")):
        target = file.resolve()
        if file.is_symlink() and not (target.is_file() and target.is_relative_to((root or path).resolve())):
            msg = f"{file} is a link to {target}: only files of {root or path} are"
            raise ValueError(msg)
        data, executable = target.read_bytes(), bool(target.stat().st_mode & stat.S_IXUSR)
        h.update(f"{file.relative_to(path)}\0{executable}\0".encode())
        h.update(hashlib.sha256(data).digest())
        if stage:
            stage(file.relative_to(path).as_posix(), data, executable)
    return h.hexdigest()


def target(arch: str) -> str:
    """Return the rpm target of an RPM architecture: our packages are built for x86_64 on x86_64_v2."""
    return f"{arch.removesuffix('_v2')}-linux"


def load_packages(project: Project) -> dict[str, Package]:
    """Return the metadata of all the packages of `project`, by name, for its architecture."""
    dirs = list(project.package_dirs.values())
    hashes = [(d.name, hash_dir(d, project.root)) for d in dirs]
    native = project.on_host()
    cache = (
        project.state
        / "cache/specs"
        / project.arch
        / (
            hashlib.sha256(
                json.dumps([*_SCRIPTS, image_tag(native), target(project.arch), *hashes]).encode()
            ).hexdigest()
            + ".json"
        )
    )
    if not cache.exists():
        build_image(native)
        specs = " ".join(f"/project/{main_spec(d).relative_to(project.root)}" for d in dirs)
        with project.work("specs") as work:
            run(
                native,
                "specs",
                f"python3 /xcraft/parse.py /work/specs.json {target(project.arch)} {specs}",
                volumes=[f"{project.root}:/project:ro", f"{work}:/work"],
                show="parsing the specs",
                network=False,
            )
            cache.parent.mkdir(parents=True, exist_ok=True)
            (work / "specs.json").rename(cache)
    packages = _Packages.validate_json(cache.read_bytes())
    for name, package in packages.items():
        package.name = name
    return packages
