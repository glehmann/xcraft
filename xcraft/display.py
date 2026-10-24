"""Terminal output: colorized rpmbuild logs."""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

from rich.console import Console
from rich.progress import Progress, ProgressColumn, SpinnerColumn, Task, TextColumn
from rich.progress_bar import ProgressBar
from rich.table import Column
from rich.text import Text

from xcraft import process

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Iterator
    from pathlib import Path

console = Console(highlight=False, soft_wrap=True)  # no wrapping, for logs and pipes

_STEP_RE = re.compile(r"^Executing\(%(\w+)\):")
_ERROR_RE = re.compile(r"\b(?:error|errors):|\bFAILED\b", re.IGNORECASE)
_WARNING_RE = re.compile(r"\b(?:warning|warnings):", re.IGNORECASE)
_RPMBUILD_DIAGNOSTICS_RE = re.compile(r"^RPM build (?:warnings?|errors?):\s*$")
# the errors, of rpm or the build tools, and rpm's warnings, not the many ones of the compilers
_DIAGNOSTIC_RE = re.compile(r"\b(?:error|errors):|\bFAILED\b|^warning:")
_RULES = [
    (_ERROR_RE, "bold red"),
    (_WARNING_RE, "yellow"),
    (re.compile(r"Executing\(%\w+\)"), "bold cyan"),
    (re.compile(r"^\+\s+.*"), "dim cyan"),
    (re.compile(r"Wrote:\s+\S+\.rpm"), "bold green"),
    (re.compile(r"Processing files:|Finding Provides:|Finding Requires"), "magenta"),
]


def _colorize(line: str) -> Text:
    text = Text.from_ansi(line.rstrip("\n"))
    for pattern, style in _RULES:
        for m in pattern.finditer(text.plain):
            text.stylize(style, m.start(), m.end())
    return text


def print_log_line(line: str) -> None:
    """Print a line of build log with colors, keeping the ones of the build tools."""
    console.print(_colorize(line))


def diagnostics(on_line: Callable[[str], None], header: str = "") -> Callable[[str], None]:
    """Return a function giving to `on_line` the errors and warnings among the lines of build log it gets.

    Those are the lines with ``error:`` or ``FAILED``, those of rpm's warnings, starting
    with ``warning:``, and the indented ones under the ``RPM build errors:`` and ``RPM
    build warnings:`` of rpmbuild. `header` is given before the first of them, if any.
    """
    under_rpmbuild = False

    def filter_line(line: str) -> None:
        nonlocal under_rpmbuild, header
        plain = Text.from_ansi(line).plain.rstrip()
        if _RPMBUILD_DIAGNOSTICS_RE.match(plain):
            under_rpmbuild = True
        elif not (under_rpmbuild and plain[:1].isspace()):
            under_rpmbuild = False
            if not _DIAGNOSTIC_RE.search(plain):
                return
        if plain.strip():
            if header:
                on_line(header)
                header = ""
            on_line(line)

    return filter_line


def page_log(lines: Iterable[str]) -> None:
    """Show log lines with colors, through ``$PAGER`` (``less`` by default) when on a terminal and available.

    The lines are sent to the pager as they come, and not read anymore once it is quit.
    """
    pager = shlex.split(os.environ.get("PAGER", "less"))
    if not console.is_terminal or not pager or not shutil.which(pager[0]):
        for line in lines:
            print_log_line(line)
        return
    # like git: colors, no paging when the log fits on the screen
    env = {"LESS": "FRX", **os.environ}
    with (
        contextlib.suppress(BrokenPipeError),  # the pager was quit
        process.popen(pager, stdin=subprocess.PIPE, env=env) as proc,
    ):
        out = Console(file=proc.stdin, force_terminal=True, width=console.width, highlight=False, soft_wrap=True)
        for line in lines:
            out.print(_colorize(line))


def follow_log(path: Path, running: Callable[[], bool], interval: float = 0.3) -> Iterator[str]:
    """Yield the lines added to `path` from now on, until `running()` is false and nothing is added anymore."""
    offset = path.stat().st_size if path.exists() else 0
    pending = b""
    while True:
        size = path.stat().st_size if path.exists() else 0
        if size < offset:  # a new build started over
            offset, pending = 0, b""
        if size == offset:
            if not running():
                return
            time.sleep(interval)
            continue
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read()
        offset += len(data)
        *lines, pending = (pending + data).split(b"\n")
        yield from (line.decode(errors="replace") for line in lines)


def read_tail(path: Path, size: int = 2**20) -> list[str]:
    """Return the lines of the last `size` bytes of `path`, without the partial first one."""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - size))
        lines = f.read().decode(errors="replace").splitlines()
    return lines[1:] if path.stat().st_size > size else lines


def log_steps(lines: list[str]) -> list[str]:
    """Return the names of the rpmbuild steps in a log, in order."""
    return list(dict.fromkeys(m.group(1) for line in lines if (m := _STEP_RE.match(line))))


def log_step(lines: list[str], step: str = "last") -> list[str]:
    """Return the lines of an rpmbuild step (``prep``, ``build``...), the last one by default.

    Lines before the first step are returned when the log has no step at all.
    """
    starts = [(i, m.group(1)) for i, line in enumerate(lines) if (m := _STEP_RE.match(line))]
    if not starts:
        return lines if step == "last" else []
    matching = [n for n, (_, name) in enumerate(starts) if step in ("last", name)]
    if not matching:
        return []
    n = matching[-1]
    return lines[starts[n][0] : starts[n + 1][0] if n + 1 < len(starts) else None]


def changes(before: set[str], after: set[str]) -> list[str]:
    """Return what `after` has but not `before`, as ``+<it>``, then what it lost, as ``-<it>``."""
    return [*(f"+{e}" for e in sorted(after - before)), *(f"-{e}" for e in sorted(before - after))]


def duration(seconds: float) -> str:
    """Return a duration like 2m30s: hours and minutes, minutes and seconds, or seconds."""
    hours, rest = divmod(round(seconds), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}h{minutes:02}m" if hours else f"{minutes}m{seconds:02}s" if minutes else f"{seconds}s"


class _Elapsed(ProgressColumn):
    """The elapsed time, like the estimates of the durations."""

    def render(self, task: Task) -> Text:
        return Text(duration(task.elapsed or 0), style="progress.elapsed")


class _ExpectedBar(ProgressColumn):
    """A bar of the elapsed time over an expected duration, pulsing once it is over."""

    def __init__(self, expected: float) -> None:
        super().__init__(table_column=Column(width=30, no_wrap=True))
        self.expected = expected

    def render(self, task: Task) -> ProgressBar:
        elapsed = task.elapsed or 0
        over = elapsed > self.expected
        return ProgressBar(
            total=None if over else self.expected,
            completed=elapsed,
            width=30,
            pulse=over,
            # not rich's default, red enough to look like an error
            complete_style="blue",
            pulse_style="blue",
        )


@contextlib.contextmanager
def activity(description: str, expected: float | None = None) -> Generator[Callable[[str], None]]:
    """Show `description` with the elapsed time, and the last line given to the returned function.

    With an `expected` duration in seconds, a bar shows the elapsed time over it.
    """
    if not console.is_terminal:
        yield lambda _: None
        return
    with Progress(
        SpinnerColumn(),
        TextColumn("{task.description}"),
        *([_ExpectedBar(expected)] if expected else []),
        _Elapsed(),
        # the space left, not to resize the other columns as the lines come
        TextColumn("{task.fields[line]}", style="dim", markup=False, table_column=Column(ratio=1, no_wrap=True)),
        console=console,
        transient=True,
        expand=True,
    ) as progress:
        task = progress.add_task(description, line="")
        yield lambda line: progress.update(task, line=Text.from_ansi(line).plain.strip()[:100])


def download_progress(on_line: Callable[[str], None], prefix: str = "") -> Callable[[int, int, int], None]:
    """Return a ``urlretrieve`` report hook giving the downloaded size to `on_line`, after `prefix`."""

    def report(blocks: int, block_size: int, total: int) -> None:
        size = blocks * block_size / 2**20
        if total > 0:
            on_line(f"{prefix}{min(size, total / 2**20):.1f}/{total / 2**20:.1f} MiB")
        else:
            on_line(f"{prefix}{size:.1f} MiB")

    return report
