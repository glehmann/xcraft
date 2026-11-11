"""rpmlint, with Fedora's rules, on the specs of our packages and on their binary packages, in the lint image.

The configuration of the project is in lint/, out of the package directories, which stay as imported:
``rpmlint.toml`` for all the packages, and ``<package>.rpmlintrc`` for the filters of one, after those of its upstream
rpmlintrc files, in its directory, if any. rpmlint runs once per package, for the filters to be its own only, all in
one container, natively: what it finds in the binary packages does not depend on their architecture. The filters
unused, like those of the binary packages when linting a spec, are not reported.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import TYPE_CHECKING

from rich.text import Text

from xcraft import container
from xcraft.container import side_image
from xcraft.display import console
from xcraft.packages import is_debug
from xcraft.specs import main_spec

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project

LINT_DIR = "lint"
"""The directory of the configuration of rpmlint, in the project."""

_FINDING = re.compile(r"\S+: ([EW]): ")
"""A line of what rpmlint found: an error or a warning, after the package, or the spec and its line."""


@dataclass
class Findings:
    """What rpmlint found in the files of a package: the `lines` of its errors and warnings, and whether it `failed`,
    with errors, or without running.
    """

    lines: list[str]
    failed: bool


def spec(project: Project, name: str) -> Path:
    """Return the spec of the package `name`."""
    return main_spec(project.package_dirs[name])


def binaries(rpms: Path) -> list[Path]:
    """Return the binary packages in the directory `rpms`, of a build, but the debug ones, long to lint for little."""
    return sorted(p for p in rpms.rglob("*.rpm") if not is_debug(p.name.rsplit("-", 2)[0]))


_FILTERS = 'addFilter(": E: unknown-key ")\n'
"""The filters of all the packages: the signatures of the builds, which xcraft checks, by keys rpmlint does not have."""


def _rpmlintrc(project: Project, name: str) -> str:
    """Return the filters of the package `name`: xcraft's, those of its upstream rpmlintrc files, then the project's."""
    files = [*sorted(project.package_dirs[name].glob("*rpmlintrc")), project.root / LINT_DIR / f"{name}.rpmlintrc"]
    return _FILTERS + "".join(f.read_text() + "\n" for f in files if f.is_file())


def _findings(output: str, status: int) -> Findings:
    """Return the findings of the `output` of rpmlint, which exited with `status`: 64 for errors, 66 for a badness
    over the threshold, others when it could not run, whose output is kept whole.
    """
    lines = [line for line in output.splitlines() if _FINDING.match(line)]
    if status not in (0, 64, 66):
        lines += ["rpmlint failed:", *output.splitlines()[-20:]]
    return Findings(lines, status != 0)


def findings(project: Project, files: dict[str, list[Path]], *, show: str) -> dict[str, Findings]:
    """Return what rpmlint finds in the `files` of each package, its spec, or its binary packages, by package,
    showing `show` while it runs.
    """
    native = project.on_host()
    image = side_image(native, "lint")
    config = project.root / LINT_DIR / "rpmlint.toml"
    mounts = {d: f"/in/{i}" for i, d in enumerate(sorted({f.parent for paths in files.values() for f in paths}))}
    with project.tmp_dir("lint") as work:
        script = []
        for name, paths in files.items():
            (work / f"{name}.rpmlintrc").write_text(_rpmlintrc(project, name))
            cmd = ["rpmlint", "--ignore-unused-rpmlintrc", "-r", f"/work/{name}.rpmlintrc"]
            cmd += ["-c", "/rpmlint.toml"] if config.exists() else []
            cmd += [f"{mounts[f.parent]}/{f.name}" for f in paths]
            script.append(f"{shlex.join(cmd)} > /work/{name}.out 2>&1; echo $? > /work/{name}.status")
        container.run(
            native,
            "lint",
            "\n".join(script),
            volumes=[
                f"{work}:/work",
                *(f"{d}:{m}:ro" for d, m in mounts.items()),
                *([f"{config}:/rpmlint.toml:ro"] if config.exists() else []),
            ],
            show=show,
            image=image,
        )
        return {
            name: _findings((work / f"{name}.out").read_text(), int((work / f"{name}.status").read_text()))
            for name in files
        }


def show_findings(found: dict[str, Findings]) -> None:
    """Show the lines of the `found` findings, the errors in red, the warnings in yellow."""
    for f in found.values():
        for line in f.lines:
            match = _FINDING.match(line)
            style = {"E": "red", "W": "yellow"}[match[1]] if match else "red"
            console.print(Text(line, style=style))
