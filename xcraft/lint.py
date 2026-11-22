"""rpmlint, with Fedora's rules, on the specs of our packages and on their binary packages, in the lint image.

The configuration of the project is in lint/, out of the package directories, which stay as imported: ``rpmlint.toml``
for all the packages, but ReleaseExtension, xcraft's, and ``<package>.rpmlintrc`` for the filters of one, after those of
its upstream rpmlintrc files, in its directory, if any. rpmlint runs once per package, for the filters to be its own
only, all in one container, natively: what it finds in the binary packages does not depend on their architecture. The
filters unused, like those of the binary packages when linting a spec, are not reported.
"""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from xcraft import container
from xcraft.container import side_image
from xcraft.display import activity, console, messages, tagged
from xcraft.packages import is_debug
from xcraft.specs import main_spec

if TYPE_CHECKING:
    from rich.console import Console

    from xcraft.project import Project

LINT_DIR = "lint"
"""The directory of the configuration of rpmlint, in the project."""

_FINDING = re.compile(r"([^\s:]+)((?::\d+)?: ([EW]): .*)")
"""A line of what rpmlint found: an error or a warning, after the package, or the name of the spec and its line."""


def spec(project: Project, name: str) -> Path:
    """Return the spec of the package `name`."""
    return main_spec(project.package_dirs[name])


def binaries(rpms: Path) -> list[Path]:
    """Return the binary packages in the directory `rpms`, of a build, but the debug ones, long to lint for little."""
    return sorted(p for p in rpms.rglob("*.rpm") if not is_debug(p.name.rsplit("-", 2)[0]))


_CONFIG = r"""ReleaseExtension = '\+b\d.*$'
"""
"""The configuration of all the projects: the latest changelog entry compared with the version-release of the packages
without what %{autorev}, the release suffix of their builds, starts: the changelogs do not have it, nor the dist tag
after it.
"""

_FILTERS = 'addFilter(": E: unknown-key ")\n'
"""The filters of all the packages: the signatures of the builds, which xcraft checks, by keys rpmlint does not have."""


def _rpmlintrc(project: Project, name: str) -> str:
    """Return the filters of the package `name`: xcraft's, those of its upstream rpmlintrc files, then the project's."""
    files = [*sorted(project.package_dirs[name].glob("*rpmlintrc")), project.root / LINT_DIR / f"{name}.rpmlintrc"]
    return _FILTERS + "".join(f.read_text() + "\n" for f in files if f.is_file())


_MARK = "@@xcraft-lint "
"""The line of the script before the output of rpmlint for a package, with its name."""


def _shown(path: Path) -> str:
    """Return how to show `path`, to open it from the terminal: relative to the current directory when in it."""
    return str(path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path)


def findings(
    project: Project, files: dict[str, list[Path]], *, show: str, tag: bool = True, width: int = 0
) -> dict[str, bool]:
    """Return whether rpmlint finds errors in the `files` of each package, its spec, or its binary packages, or fails
    to run, by package, showing `show` with the package linted while it runs, and what it finds as it comes, after the
    name of the package, padded to `width`, or to the longest name of `files`, unless not `tag`, like for the editors,
    colored like the build logs.
    """
    native = project.on_host()
    image = side_image(native, "lint")
    config = project.root / LINT_DIR / "rpmlint.toml"
    mounts = {d: f"/in/{i}" for i, d in enumerate(sorted({f.parent for paths in files.values() for f in paths}))}
    specs = {f.name: _shown(f) for paths in files.values() for f in paths if f.suffix == ".spec"}
    with project.tmp_dir("lint") as work, activity(show) as progress:
        # one directory, as rpmlint takes one -c: its files load in no given order, hence xcraft's ReleaseExtension only
        (work / "config").mkdir()
        (work / "config/xcraft.toml").write_text(_CONFIG)
        if config.exists():
            shutil.copy(config, work / "config/rpmlint.toml")
        script = []
        for i, (name, paths) in enumerate(files.items(), 1):
            (work / f"{name}.rpmlintrc").write_text(_rpmlintrc(project, name))
            cmd = ["rpmlint", "--ignore-unused-rpmlintrc", "-r", f"/work/{name}.rpmlintrc", "-c", "/work/config"]
            cmd += [f"{mounts[f.parent]}/{f.name}" for f in paths]
            script.append(f"echo {shlex.quote(f'{_MARK}{name} ({i}/{len(files)})')}")
            script.append(
                f"{shlex.join(cmd)} 2>&1 | tee /work/{name}.out; echo ${{PIPESTATUS[0]}} > /work/{name}.status"
            )

        width, shown = width or max(map(len, files), default=0), tagged("", 0, _out())

        def on_line(line: str) -> None:
            nonlocal shown
            if line.startswith(_MARK):
                progress(current := line.removeprefix(_MARK))
                # after the name of the package, like the warnings of the builds
                shown = tagged(current.rsplit(" (", 1)[0] if tag else "", width, _out())
            elif match := _FINDING.match(line.rstrip("\n")):
                shown(f"{specs.get(match[1], match[1])}{match[2]}")

        container.run(
            native,
            "lint",
            "\n".join(script),
            volumes=[f"{work}:/work", *(f"{d}:{m}:ro" for d, m in mounts.items())],
            on_line=on_line,
            image=image,
        )
        return {name: _failed(work, name) for name in files}


def _out() -> Console:
    """Return the console of the findings, the results: the messages' when both are the terminal, for them to show
    above the progress, which other consoles would overwrite.
    """
    return messages if messages.is_interactive and console.is_terminal else console


def _failed(work: Path, name: str) -> bool:
    """Return whether rpmlint found errors for `name`, its output and status in `work`: 64 for errors, 66 for a badness
    over the threshold, others when it could not run, whose output is shown.
    """
    if (status := int((work / f"{name}.status").read_text())) not in (0, 64, 66):
        lines = (work / f"{name}.out").read_text().splitlines()[-20:]
        _out().print(Text("\n".join([f"rpmlint failed for {name}:", *lines]), style="red"))
    return status != 0
