"""xcraft contents."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape
from rich.table import Table

from xcraft import contents as snapshot
from xcraft import resolution
from xcraft.commands import Arch, prepared_project
from xcraft.display import console, print_wide

if TYPE_CHECKING:
    from xcraft.contents import Change, Contents
    from xcraft.project import Project

_BINARY_SETS = ("base", "build", "iso", "install.img")


def _table(contents: Contents) -> Table:
    """Return the table of the packages of all the sets, by source package, ours first, with the sets of each."""
    kinds = [k for k in _BINARY_SETS if k in contents.sets]
    binaries: dict[str, dict[str, snapshot.Entry]] = {}
    for kind in kinds:
        for name, entry in contents.sets[kind].items():
            binaries.setdefault(entry.source, {})[name] = entry
    sources = {n: e for k in ("base/Source", "build/Source") for n, e in contents.sets.get(k, {}).items()}
    table = Table("package", "version", *kinds, box=None, pad_edge=False, header_style="bold")
    for source in sorted(binaries, key=lambda s: (not next(iter(binaries[s].values())).ours, s)):
        marks = ["✓" if source in contents.sets.get(f"{k}/Source", {}) else "" for k in kinds]
        table.add_row(f"[bold]{source}[/] (source)", sources[source].evr if source in sources else "", *marks)
        for name, entry in sorted(binaries[source].items()):
            table.add_row(f"  {name}", entry.evr, *("✓" if name in contents.sets[k] else "" for k in kinds))
    return table


def _show(contents: Contents) -> None:
    print_wide(_table(contents))
    for kind, packages in contents.sets.items():
        ours = sum(e.ours for e in packages.values())
        console.print(f"{kind}: {len(packages)} packages, {ours} ours, {len(packages) - ours} upstream")


def _show_why(contents: Contents, name: str) -> None:
    if not (paths := snapshot.why(contents, name)):
        msg = f"{name} is in no set of binary packages of the snapshot"
        raise typer.BadParameter(msg)
    by_path: dict[tuple[str, ...], list[str]] = {}
    for kind, steps in paths.items():
        by_path.setdefault(tuple(steps), []).append(kind)
    for steps, kinds in by_path.items():
        console.print(escape(f"{', '.join(kinds)}: {', '.join([name, *steps]) if steps else f'{name}, not known'}"))


def _change(contents: Contents, kind: str, change: Change, *, markdown: bool) -> str:
    """Return the line of a `change` of the set `kind`, of the new `contents`."""
    name = f"`{change.name}`" if markdown else change.name
    source = f" (source {change.entry.source})" if change.entry.source != change.name else ""
    if change.old is None:
        why = f", {why}" if (why := snapshot.reason(contents, kind, change.name)) else ""
        return f"{'- added' if markdown else '  +'} {name} {change.entry.evr}{source}{why}"
    if change.new is None:
        return f"{'- removed' if markdown else '  -'} {name} {change.old.evr}{source}"
    return f"{'- updated' if markdown else '  ~'} {name} {change.old.evr} → {change.new.evr}{source}"


_COUNTS = {
    "added": lambda c: c.old is None,
    "updated": lambda c: c.old is not None and c.new is not None,
    "removed": lambda c: c.new is None,
}


def _show_diff(old: Contents, new: Contents, only: str | None, *, markdown: bool) -> None:
    """Show what changed from `old` to `new`, by set: of the binary sets, or `only` one, as Markdown with `markdown`."""
    lines = []
    for kind, changed in snapshot.changes(old, new).items():
        if (only or kind) != kind or (not only and kind.endswith("/Source")):
            continue
        counts = {what: sum(test(c) for c in changed) for what, test in _COUNTS.items()}
        title = f"{kind}: " + ", ".join(f"{n} {what}" for what, n in counts.items() if n)
        lines += ["", f"**{title}**" if markdown else title]
        lines += [_change(new, kind, c, markdown=markdown) for c in changed]
    text = "\n".join(lines[1:]) if lines else "no change of the packages"
    if markdown:
        console.file.write(text + "\n")
    else:
        console.print(escape(text))


def _published(project: Project, stage: str) -> Contents:
    """Return the contents of the published snapshot `stage`, as <remote>@<stage>[/<snapshot>]."""
    if not (match := re.fullmatch(r"([^@/]+)@([^@/]+)(?:/([^@/]+))?", stage)):
        msg = "a published snapshot, as <remote>@<stage>[/<snapshot>], like main@incoming or main@incoming/latest"
        raise typer.BadParameter(msg, param_hint="--diff-stage")
    name, stage, name_of_snapshot = match.groups()
    if not (storage := next((s for n, s in project.config.remote.items() if n == name), None)):
        msg = f"no {name} remote storage: see [remote.{name}] in xcraft.toml"
        raise typer.BadParameter(msg, param_hint="--diff-stage")
    with project.tmp_dir("contents") as work:
        return snapshot.published(project, storage, stage, name_of_snapshot or "latest", work)


def contents(
    only: Annotated[
        str | None,
        typer.Option(
            "--set",
            help=f"Show only this set, as a list of NEVRAs: {', '.join(snapshot.SETS)}.",
            show_default=False,
        ),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Show all the sets, as JSON.")] = False,
    why: Annotated[
        str | None,
        typer.Option(help="Show why this binary package is in each set it is in.", show_default=False),
    ] = None,
    diff: Annotated[
        str | None,
        typer.Option(help="Show what changed since this git revision, like master.", show_default=False),
    ] = None,
    diff_stage: Annotated[
        str | None,
        typer.Option(
            help="Show what changed since a published snapshot, <remote>@<stage>[/<snapshot>], like main@incoming.",
            show_default=False,
        ),
    ] = None,
    markdown: Annotated[bool, typer.Option(help="Show the changes as Markdown, for a comment.")] = False,
    arch: Arch = None,
) -> None:
    """Show the packages the snapshot of the checkout would have, without building: by set, its repositories, base and
    build, and their source packages, and the repository of its ISO and install.img, with their versions.

    Our packages have their final versions, as once on the main branch. The debuginfo and debugsource packages are not
    shown.
    """
    if only and only not in snapshot.SETS:
        msg = f"unknown set: {only}, not one of {', '.join(snapshot.SETS)}"
        raise typer.BadParameter(msg, param_hint="--set")
    if diff and diff_stage:
        msg = "--diff and --diff-stage compare with one snapshot only"
        raise typer.BadParameter(msg)
    project = prepared_project(arch=arch)
    current = snapshot.contents(project)
    if diff or diff_stage:
        if diff:
            with snapshot.checkout(project, diff) as other:
                resolution.prepare(other)
                old = snapshot.contents(other)
        else:
            old = _published(project, diff_stage or "")
        _show_diff(old, current, only, markdown=markdown)
    elif why:
        _show_why(current, why)
    elif as_json:
        console.file.write(current.model_dump_json(indent=1) + "\n")
    elif only:
        for name, entry in current.sets.get(only, {}).items():
            console.file.write(f"{name}-{entry.evr}.{entry.arch}\n")
    else:
        _show(current)
