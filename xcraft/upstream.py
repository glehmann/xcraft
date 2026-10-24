"""The requirements between the upstream packages of the lock, resolved in the build environment."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import BaseModel

from xcraft.container import BUILDENV, DNF_LOCK, build_image, image_tag, run
from xcraft.resolution import resolve_image
from xcraft.specs import dep_name

if TYPE_CHECKING:
    from xcraft.project import Project


class UpstreamRequires(BaseModel):
    """The requirements between the locked upstream packages, by name."""

    requires: dict[str, list[str]]
    """For each upstream package, the upstream packages providing its Requires."""
    providers: dict[str, list[str]]
    """For each capability required by our packages and not provided by them, the upstream packages providing it."""


def upstream_requires(project: Project) -> UpstreamRequires:
    """Return the requirements between the locked upstream packages, and with our packages."""
    packages = project.packages
    ours = {
        dep_name(c)
        for package in packages.values()
        for binary in package.binaries
        for c in [binary.name, *binary.provides]
    }
    capabilities = {
        r
        for package in packages.values()
        for r in [*package.buildrequires, *(r for b in package.binaries for r in b.requires)]
        if dep_name(r) not in ours
    }
    data = json.dumps(
        {
            "packages": {name: [p.evr, p.arch] for name, p in sorted(resolve_image(project).packages.items())},
            "capabilities": sorted(capabilities),
        }
    )

    script = (BUILDENV / "requires.py").read_text()
    key = hashlib.sha256(json.dumps([script, image_tag(project), data]).encode()).hexdigest()
    cache = project.state / "cache/upstream" / f"{key}.json"
    if not cache.exists():
        build_image(project)
        with project.work("upstream") as work:
            (work / "input.json").write_text(data)
            run(
                project,
                "upstream",
                f"{DNF_LOCK} python3 /xcraft/requires.py /work/input.json /work/output.json",
                volumes=[f"{work}:/work"],
                show="resolving the requirements of the upstream packages",
            )
            cache.parent.mkdir(parents=True, exist_ok=True)
            (work / "output.json").rename(cache)
    return UpstreamRequires.model_validate_json(cache.read_bytes())
