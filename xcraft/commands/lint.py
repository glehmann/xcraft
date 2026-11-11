"""xcraft lint."""

from __future__ import annotations

import typer

from xcraft import buildkey
from xcraft import lint as linter
from xcraft.cache import cached_build
from xcraft.commands import Arch, Dev, Final, Packages, dev_ident, prepared_project
from xcraft.devbuilds import dev_keys
from xcraft.display import console


def lint(
    ctx: typer.Context, packages: Packages = None, dev: Dev = None, final: Final = False, arch: Arch = None
) -> None:
    """Lint the specs of the given packages, all by default, and the binary packages of their builds, when built, with
    rpmlint and Fedora's rules: the development builds, unless --final, see --dev. Fails on errors.

    The configuration is in lint/: rpmlint.toml for all the packages, and <package>.rpmlintrc for the filters of one,
    after those of its upstream rpmlintrc files, in its directory.
    """
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
