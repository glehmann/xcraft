"""xcraft status."""

from __future__ import annotations

import time
from collections import Counter

from rich.measure import Measurement
from rich.table import Table

from xcraft import build as builder
from xcraft.cache import build_keys, cached_build, has_normal_build, inputs
from xcraft.commands import Arch, Packages, load
from xcraft.display import console
from xcraft.testbuilds import TestBuild, test_builds

_STYLES = {
    "error": "red",
    "building": "yellow",
    "queued": "cyan",
    "built": "green",
    "outdated": "magenta",
    "missing": "dim",
}


def _age(timestamp: float) -> str:
    seconds = time.time() - timestamp
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def _tests(builds: list[TestBuild]) -> str:
    return ", ".join(
        f"{b.ident}.{b.number} ({b.kind}, {_age(b.built)}{'' if b.current else ', [yellow]outdated[/]'})"
        for b in builds
    )


def status(packages: Packages = None, arch: Arch = None) -> None:
    """Show the version of the given packages, or all, and whether it is built.

    The states: error (the spec cannot be parsed), building, queued (by a running
    build), built, outdated (only older versions are built), and missing. The
    tests column shows the latest test or pre build of each ID, outdated when the
    package or its dependencies changed since.
    """
    project = load(packages, arch=arch)
    hashes = inputs(project)
    keys = build_keys(project, hashes=hashes)
    queue = [name for name in builder.queued(project) if not cached_build(project, name, keys[name]).exists()]
    shown = sorted(set(packages or project.packages))
    tests = {name: test_builds(project, name, hashes) for name in shown}
    columns = ["package", "version", "state"] + (["tests"] if any(tests.values()) else [])
    table = Table(*columns, box=None, pad_edge=False, header_style="bold")
    states = Counter[str]()
    for name in shown:
        package = project.packages[name]
        evr = package.binaries[0].evr if package.binaries else ""
        if package.error:
            state = "error"
        elif builder.building(project, name):
            state = "building"
        elif name in queue:
            state = "queued"
        elif cached_build(project, name, keys[name]).exists():
            state = "built"
        else:
            state = "outdated" if has_normal_build(project, name) else "missing"
        states[state] += 1
        detail = f" {queue.index(name) + 1}/{len(queue)}" if state == "queued" else ""
        table.add_row(
            name, evr, f"[{_STYLES[state]}]{state}{detail}[/]", *([_tests(tests[name])] if "tests" in columns else [])
        )
    # one line per package, for grep, even wider than the terminal
    width = console.width
    console.width = max(width, Measurement.get(console, console.options.update(width=10_000), table).maximum)
    try:
        console.print(table)
    finally:
        console.width = width
    console.print(", ".join(f"[{_STYLES[state]}]{states[state]} {state}[/]" for state in _STYLES if states[state]))
