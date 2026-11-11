"""Package metadata, extracted from the specs by rpm in the build environment.

All the specs are parsed in a single container, so that macros are expanded
and conditionals evaluated as in a build: natively, in the build environment of
the host, for the target architecture, like rpmbuild --target. The result is
cached until a package directory or the build environment image changes.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from xcraft import container
from xcraft.container import BUILDENV, built_image, image_tag
from xcraft.packages import Package

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from xcraft.project import Project


_SCRIPTS = [(BUILDENV / script).read_text() for script in ("parse.py", "rpmdeps.py")]
"""The parser of the specs, in the cache key of what it parsed."""


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


def rpm_target(arch: str) -> str:
    """Return the rpm target of an RPM architecture: our packages are built for x86_64 on x86_64_v2."""
    return f"{arch.removesuffix('_v2')}-linux"


_read: set[Path] = set()
"""The cached metadata of the specs read by this process, by file."""


def parse(project: Project, dirs: list[Path]) -> dict[str, Package]:
    """Return the metadata of the packages of the directories `dirs`, by name, for the architecture of `project`."""
    hashes = [(d.name, hash_dir(d, project.root)) for d in dirs]
    native = project.on_host()
    key = json.dumps([*_SCRIPTS, image_tag(native), rpm_target(project.arch), *hashes])
    cache = project.state / "cache/specs" / project.arch / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    if not cache.exists():
        built_image(native)
        specs = " ".join(f"/project/{main_spec(d).relative_to(project.root)}" for d in dirs)
        with project.tmp_dir("specs") as work:
            container.run(
                native,
                "specs",
                f"python3 /xcraft/parse.py /work/specs.json {rpm_target(project.arch)} {specs}",
                volumes=[f"{project.root}:/project:ro", f"{work}:/work"],
                show="parsing the specs",
            )
            cache.parent.mkdir(parents=True, exist_ok=True)
            (work / "specs.json").rename(cache)
    _read.add(cache)
    os.utime(cache)  # used, see xcraft clean --keep-days
    packages = _Packages.validate_json(cache.read_bytes())
    for name, package in packages.items():
        package.name = name
    return packages


def load_packages(project: Project) -> dict[str, Package]:
    """Return the metadata of all the packages of `project`, by name, for its architecture: a binary package of
    several of them is an error, which would make two packages of a name, and their records ambiguous.
    """
    packages = parse(project, list(project.package_dirs.values()))
    owners: dict[str, list[str]] = {}
    for name, package in packages.items():
        for b in package.binaries:
            owners.setdefault(b.name, []).append(name)
    if shared := sorted(f"{b} ({', '.join(names)})" for b, names in owners.items() if len(names) > 1):
        msg = f"binary packages of several packages for {project.arch}: {'; '.join(shared)}"
        raise RuntimeError(msg)
    return packages


def specs_read() -> set[Path]:
    """Return the files of the cached metadata of the specs this process read: those of the specs as they are."""
    return set(_read)
