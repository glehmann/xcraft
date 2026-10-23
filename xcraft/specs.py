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
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter

from xcraft.container import build_image, image_tag, run

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project


def dep_name(dep: str) -> str:
    """Return the capability of a dependency, like ``foo`` for ``foo >= 1.0``, or a rich one whole."""
    return dep if dep.startswith("(") else dep.split(" ", 1)[0]


# Run by the build environment's python, with the rpm bindings.
_PARSER = """
import json, pathlib, rpm, subprocess, sys

out, target, *spec_files = sys.argv[1:]
result = {}


def deps(h, tag):
    # like "foo >= 1:2-3", or a rich one, like "(a if b)"
    return [d.DNEVR()[2:] for d in rpm.ds(h, tag) if not d.N().startswith("rpmlib(")]


for spec_file in spec_files:
    pkg_dir = pathlib.Path(spec_file).parent
    # for the target architecture, like rpmbuild --target
    rpm.reloadConfig(target)
    rpm.addMacro("_sourcedir", str(pkg_dir))
    rpm.addMacro("_specdir", str(pkg_dir))
    try:
        spec = rpm.spec(spec_file)
    except ValueError:
        # parse again with rpmspec, to get the error message
        error = subprocess.run(["rpmspec", "-D", f"_sourcedir {pkg_dir}", "-q", spec_file],
                               capture_output=True, text=True).stderr
        result[pkg_dir.name] = {"error": error.strip() or "cannot parse spec"}
        continue
    result[pkg_dir.name] = {
        "binaries": [
            {
                "name": h[rpm.RPMTAG_NAME],
                "evr": h[rpm.RPMTAG_EVR],
                "arch": h[rpm.RPMTAG_ARCH],
                "provides": deps(h, rpm.RPMTAG_PROVIDENAME),
                "requires": deps(h, rpm.RPMTAG_REQUIRENAME),
                "recommends": deps(h, rpm.RPMTAG_RECOMMENDNAME),
            }
            for h in (p.header for p in spec.packages)
        ],
        "buildrequires": deps(spec.sourceHeader, rpm.RPMTAG_REQUIRENAME),
        "dist": rpm.expandMacro("%{?dist}"),
        # sources are of type 1, patches 2
        "source0": next((source for source, n, kind in spec.sources if kind == 1 and n == 0), ""),
        "sources": [source for source, n, kind in sorted(spec.sources, key=lambda s: s[1]) if kind == 1],
    }
pathlib.Path(out).write_text(json.dumps(result))
"""


class Binary(BaseModel):
    """A binary package defined by a spec."""

    name: str
    evr: str
    arch: str
    provides: list[str]
    """Its capabilities, with their versions, like ``foo = 1:2-3``."""
    requires: list[str]
    recommends: list[str] = []


class Package(BaseModel):
    """What xcraft knows about a package directory, from its spec."""

    name: str = ""
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


def hash_dir(path: Path) -> str:
    """Return a hash of the files in `path`, as the builds get them, except its ``origin``.

    That is their paths, contents, and whether they are executable, like git:
    the rest of their modes depends on the umask.
    """
    h = hashlib.sha256()
    for file in sorted(p for p in path.rglob("*", recurse_symlinks=True) if p.is_file() and p != path / "origin"):
        h.update(f"{file.relative_to(path)}\0{os.access(file, os.X_OK)}\0".encode())
        h.update(hashlib.sha256(file.read_bytes()).digest())
    return h.hexdigest()


def target(arch: str) -> str:
    """Return the rpm target of an RPM architecture: our packages are built for x86_64 on x86_64_v2."""
    return f"{arch.removesuffix('_v2')}-linux"


def load_packages(project: Project) -> dict[str, Package]:
    """Return the metadata of all the packages of `project`, by name, for its architecture."""
    dirs = list(project.package_dirs.values())
    hashes = [(d.name, hash_dir(d)) for d in dirs]
    native = project.on_host()
    cache = (
        project.state
        / "cache/specs"
        / project.arch
        / (
            hashlib.sha256(json.dumps([_PARSER, image_tag(native), target(project.arch), *hashes]).encode()).hexdigest()
            + ".json"
        )
    )
    if not cache.exists():
        build_image(native)
        specs = " ".join(f"/project/{main_spec(d).relative_to(project.root)}" for d in dirs)
        with project.work("specs") as work:
            (work / "parse.py").write_text(_PARSER)
            run(
                native,
                "specs",
                f"python3 /work/parse.py /work/specs.json {target(project.arch)} {specs}",
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
