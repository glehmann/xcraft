"""Package metadata, extracted from the specs by rpm in the build environment.

All the specs are parsed in a single container, so that macros are expanded
and conditionals evaluated exactly as in a build. The result is cached until a
package directory or the build environment image changes.
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

# Run by the build environment's python, with the rpm bindings.
_PARSER = """
import json, pathlib, rpm, subprocess, sys

out, *spec_files = sys.argv[1:]
result = {}
for spec_file in spec_files:
    pkg_dir = pathlib.Path(spec_file).parent
    rpm.reloadConfig()
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
                "provides": h[rpm.RPMTAG_PROVIDENAME],
                "requires": h[rpm.RPMTAG_REQUIRENAME],
            }
            for h in (p.header for p in spec.packages)
        ],
        "buildrequires": [r for r in spec.sourceHeader[rpm.RPMTAG_REQUIRENAME] if not r.startswith("rpmlib(")],
        "dist": rpm.expandMacro("%{?dist}"),
        # sources are of type 1, patches 2
        "source0": next((source for source, n, kind in spec.sources if kind == 1 and n == 0), ""),
    }
pathlib.Path(out).write_text(json.dumps(result))
"""


class Binary(BaseModel):
    """A binary package defined by a spec."""

    name: str
    evr: str
    arch: str
    provides: list[str]
    requires: list[str]


class Package(BaseModel):
    """What xcraft knows about a package directory, from its spec."""

    name: str = ""
    binaries: list[Binary] = []
    buildrequires: list[str] = []
    dist: str = ""
    """The value of %{dist}, at the end of the release of the binaries."""
    source0: str = ""
    """Source0, expanded: the URL of its archive, if the spec gives it."""
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


def load_packages(project: Project) -> dict[str, Package]:
    """Return the metadata of all the packages of `project`, by name."""
    dirs = list(project.package_dirs.values())
    hashes = [(d.name, hash_dir(d)) for d in dirs]

    def cache() -> Path:
        key = hashlib.sha256(json.dumps([_PARSER, image_tag(project), *hashes]).encode()).hexdigest()
        return project.state / "cache/specs" / project.arch / f"{key}.json"

    if not cache().exists():
        build_image(project)  # which may change its tag, when it is new
        specs = " ".join(f"/project/{main_spec(d).relative_to(project.root)}" for d in dirs)
        with project.work("specs") as work:
            (work / "parse.py").write_text(_PARSER)
            run(
                project,
                "specs",
                f"python3 /work/parse.py /work/specs.json {specs}",
                volumes=[f"{project.root}:/project:ro", f"{work}:/work"],
                show="parsing the specs",
                network=False,
            )
            cache().parent.mkdir(parents=True, exist_ok=True)
            (work / "specs.json").rename(cache())
    packages = _Packages.validate_json(cache().read_bytes())
    for name, package in packages.items():
        package.name = name
    return packages
