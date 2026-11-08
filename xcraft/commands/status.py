"""xcraft status."""

from __future__ import annotations

import json
from collections import Counter
from typing import TYPE_CHECKING, Annotated

import typer
from rich.table import Table

from xcraft import build as builder
from xcraft import buildid, buildkey
from xcraft.cache import cached_build, has_normal_build, release_suffix
from xcraft.commands import Arch, Dev, Final, Packages, dev_ident, prepared_project
from xcraft.container import image_tag
from xcraft.devbuilds import dev_keys
from xcraft.display import console, print_wide

if TYPE_CHECKING:
    from xcraft.project import Project

_STYLES = {
    "error": "red",
    "building": "yellow",
    "queued": "cyan",
    "ready": "green",
    "outdated": "magenta",
    "missing": "dim",
}


Inputs = Annotated[
    bool,
    typer.Option(
        "--inputs",
        help="Show instead what the build keys of the packages are computed from, as hashed: see docs/build-keys.md.",
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
    branch's, unless --final. The states: error (the spec cannot be parsed), building, queued (by a running build),
    ready (built or fetched, in the cache), outdated (only older versions are), and missing.
    """
    ident = dev_ident(ctx, dev, final=final)
    project = prepared_project(packages, arch=arch)
    if with_inputs:
        _show_inputs(project, sorted(set(packages or project.packages)))
        return
    keys = dev_keys(project, ident, buildkey.directories(project))[0]
    queue = [name for name in builder.queued(project) if not cached_build(project, name, keys[name]).exists()]
    shown = sorted(set(packages or project.packages))
    table = Table("package", "version", "state", box=None, pad_edge=False, header_style="bold")
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
        elif builder.building(project, name):
            state = "building"
        elif name in queue:
            state = "queued"
        elif cached_build(project, name, keys[name]).exists():
            state = "ready"
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
