"""xcraft log."""

from __future__ import annotations

import re
from collections import deque
from typing import TYPE_CHECKING, Annotated

import typer

from xcraft import build as builder
from xcraft.commands import OnePackage
from xcraft.display import follow_log, log_step, log_steps, page_log, print_log_line
from xcraft.project import Project

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator
    from pathlib import Path

# completed without the log of the package: click does not parse the package
# argument when completing the value of an option
_STEPS = ["prep", "generate_buildrequires", "conf", "build", "install", "check", "doc", "license", "clean", "last"]


def _complete_step(incomplete: str) -> list[str]:
    return [step for step in _STEPS if step.startswith(incomplete)]


def log(
    package: OnePackage,
    step: Annotated[
        str | None,
        typer.Option(
            help="Only show an rpmbuild step: prep, build, install..., or last.", autocompletion=_complete_step
        ),
    ] = None,
    grep: Annotated[str | None, typer.Option(help="Only show the lines matching a regular expression.")] = None,
    last: Annotated[
        int | None, typer.Option("--last", "-n", help="Only show the last lines.", show_default=False)
    ] = None,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Show the lines added by a running build, until it ends.")
    ] = False,
) -> None:
    """Show the log of the last build of a package."""
    project = Project.find()
    path = project.log(package)
    if not path.exists() and not follow:
        msg = f"{package} has no build log"
        raise RuntimeError(msg)
    lines: Iterable[str] = _read_lines(path)
    if step:
        all_lines = list(lines)
        if not (lines := log_step(all_lines, step)):
            msg = f"no {step} step in the log of {package}, it has: {', '.join(log_steps(all_lines)) or 'none'}"
            raise RuntimeError(msg)
    if grep:
        lines = (line for line in lines if re.search(grep, line))
    if last:
        lines = deque(lines, maxlen=last)
    if not follow:
        page_log(lines)
        return
    for line in lines:
        print_log_line(line)
    for line in follow_log(path, lambda: builder.building(project, package)):
        if not grep or re.search(grep, line):
            print_log_line(line)


def _read_lines(path: Path) -> Iterator[str]:
    """Yield the lines of `path`, if it exists, read as needed: logs can be large."""
    if path.exists():
        with path.open(errors="replace") as f:
            yield from (line.rstrip("\n") for line in f)
