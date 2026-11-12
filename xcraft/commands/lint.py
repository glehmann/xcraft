"""xcraft lint."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from xcraft import buildkey
from xcraft import lint as linter
from xcraft.cache import cached_build
from xcraft.commands import Arch, Dev, Final, Packages, dev_ident, prepared_project, unprepared_project
from xcraft.devbuilds import dev_keys
from xcraft.display import console

Spec = Annotated[
    Path | None,
    typer.Option(
        "--spec",
        help="Lint only this spec, of a package, as it is on disk, without the builds, nor resolving the packages: "
        "for the editors, see docs/builds.md.",
        show_default=False,
    ),
]


def _lint_spec(spec: Path, arch: str | None) -> None:
    """Show what rpmlint finds in `spec` only, the spec of a package, and fail on errors."""
    project = unprepared_project(arch=arch)
    if not (names := [n for n, d in project.package_dirs.items() if d.resolve() == spec.resolve().parent]):
        msg = f"{spec} is not in the directory of a package"
        raise typer.BadParameter(msg)
    found = linter.findings(project, {names[0]: [spec.resolve()]}, show="linting")
    linter.show_findings(found)
    if found[names[0]].failed:
        raise typer.Exit(1)


def lint(
    ctx: typer.Context,
    packages: Packages = None,
    dev: Dev = None,
    final: Final = False,
    arch: Arch = None,
    spec: Spec = None,
) -> None:
    """Lint the specs of the given packages, all by default, and the binary packages of their builds, when built, with
    rpmlint and Fedora's rules: the development builds, unless --final, see --dev. Fails on errors.

    The configuration is in lint/: rpmlint.toml for all the packages, and <package>.rpmlintrc for the filters of one,
    after those of its upstream rpmlintrc files, in its directory.
    """
    if spec:
        if packages or final:  # not --dev, which the environment may set
            msg = "--spec lints one spec, without packages nor --final"
            raise typer.BadParameter(msg)
        _lint_spec(spec, arch)
        return
    ident = dev_ident(ctx, dev, final=final)
    project = prepared_project(packages, arch=arch)
    keys = dev_keys(project, ident, buildkey.directories(project))[0]
    names = sorted(set(packages or project.packages))
    if broken := [n for n in names if project.packages[n].error]:
        console.print(f"[yellow]specs that cannot be parsed[/]: {', '.join(broken)}")
    files, unbuilt = {}, []
    for name in (n for n in names if n not in broken):
        build = cached_build(project, name, keys[name])
        files[name] = [linter.spec(project, name), *(linter.binaries(build / "RPMS") if build.exists() else [])]
        if not build.exists():
            unbuilt.append(name)
    found = linter.findings(project, files, show="linting")
    linter.show_findings(found)
    if unbuilt:
        console.print(f"[dim]not built, only their specs linted: {', '.join(unbuilt)}[/]")
    if failed := [n for n, f in found.items() if f.failed]:
        console.print(f"[bold red]✗[/] errors in {', '.join(failed)}")
        raise typer.Exit(1)
    console.print(f"[bold green]✓[/] {len(found)} packages linted, without errors")
