"""xcraft status."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape
from rich.table import Table

from xcraft import build as builder
from xcraft import builders, buildid, buildkey
from xcraft.cache import cached_build, has_normal_build, release_suffix
from xcraft.commands import Arch, Dev, Final, Packages, dev_ident, prepared_project
from xcraft.container import image_tag
from xcraft.devbuilds import dev_keys
from xcraft.display import console, messages, print_wide

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project

_STYLES = {
    "error": "red",
    "building": "yellow",
    "queued": "cyan",
    "ready": "green",
    "built there": "blue",
    "outdated": "magenta",
    "missing": "dim",
}


Inputs = Annotated[
    bool,
    typer.Option(
        "--inputs",
        help="Show instead what the build keys of the packages are computed from, as hashed: see docs/build-keys.md.",
        hidden=True,  # for checking the keys, not for the packagers
    ),
]

Full = Annotated[bool, typer.Option("--full", help="Show the ready packages too.")]


def _show_inputs(project: Project, names: list[str]) -> None:
    """Show the build keys of the packages `names`, with their build IDs and what the keys are computed from, as JSON:
    a key is the SHA-256 of its inputs, as JSON, keys sorted, then a NUL and the name of its package. The image the
    specs were parsed in, which depends on the machine, is shown too, but not in the keys.
    """
    shown: dict[str, dict[str, list]] = {}
    ids, parsed_in = buildid.compute(project, buildid.base(project), shown=shown), image_tag(project.on_host())
    console.file.write(
        json.dumps(
            {
                name: {
                    "key": buildkey.key(shown[name], name),
                    "build ID": ids[name].text(),
                    "inputs": shown[name],
                    "specs parsed in": parsed_in,
                }
                for name in names
            },
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )


def _on_builder(project: Project, missing: dict[str, Path]) -> builders.OnBuilder:
    """Return the builds of the builder of the architecture of `project`, if any: those running, and those of the
    builds `missing` here which it made, see builders.on_builder(): the builds there, of xcraft build, are not shown
    here otherwise, until they come back.
    """
    if not (name := builders.builders().get(project.arch)):
        return builders.OnBuilder(set(), [], set())
    try:
        return builders.on_builder(project, name, missing)
    except (OSError, subprocess.CalledProcessError) as e:
        messages.print(f"[yellow]warning:[/] the builds of {name} are unknown: {escape(str(e))}")
        return builders.OnBuilder(set(), [], set())


def status(
    ctx: typer.Context,
    packages: Packages = None,
    dev: Dev = None,
    final: Final = False,
    arch: Arch = None,
    with_inputs: Inputs = False,
    full: Full = False,
) -> None:
    """Show the version of the given packages, or of all those not ready, or of all with --full, and whether its build
    is ready; and how many are in each state.

    The version has the build number, and the development build suffix of those whose build ID is not the main
    branch's, unless --final. The states: error (the spec cannot be parsed), building, queued (by a running build,
    here or on the builder of the architecture), ready (built or fetched, in the cache), built there (on the builder,
    not back yet), outdated (only older versions are), and missing.
    """
    ident = dev_ident(ctx, dev, final=final)
    project = prepared_project(packages, arch=arch)
    if with_inputs:
        _show_inputs(project, sorted(set(packages or project.packages)))
        return
    keys = dev_keys(project, ident, buildkey.directories(project))[0]
    shown = sorted(set(packages or project.packages))
    builds = {name: cached_build(project, name, keys[name]) for name in shown}
    there = _on_builder(project, {name: build for name, build in builds.items() if not build.exists()})
    queue = [
        name
        for name in [*builder.queued(project), *there.queued]
        if not cached_build(project, name, keys[name]).exists()
    ]
    table = Table(box=None, pad_edge=False, show_header=False)
    states = Counter[str]()
    for name in shown:
        package = project.packages[name]
        evr = (
            package.suffixed(e, e, release_suffix(keys[name]))
            if (e := package.binaries[0].evr if package.binaries else "")
            else ""
        )
        if package.error:
            state = "error"
        elif builder.building(project, name) or name in there.building:
            state = "building"
        elif name in queue:
            state = "queued"
        elif builds[name].exists():
            state = "ready"
        elif name in there.built:
            state = "built there"
        else:
            state = "outdated" if has_normal_build(project, name) else "missing"
        states[state] += 1
        if state == "ready" and not (full or packages):
            continue
        detail = f" {queue.index(name) + 1}/{len(queue)}" if state == "queued" else ""
        table.add_row(name, evr, f"[{_STYLES[state]}]{state}{detail}[/]")
    if table.row_count:
        print_wide(table)
    console.print(", ".join(f"[{_STYLES[state]}]{states[state]} {state}[/]" for state in _STYLES if states[state]))
