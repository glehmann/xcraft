"""xcraft build."""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Annotated

import typer

from xcraft import build as builder
from xcraft import builders, buildkey
from xcraft.cache import cached_build
from xcraft.commands import (
    Arch,
    Dev,
    Final,
    Jobs,
    Key,
    NoCheck,
    Packages,
    dev_ident,
    prepared_project,
    sign_builds,
    unprepared_project,
)
from xcraft.devbuilds import dev_keys
from xcraft.display import console, hint
from xcraft.graph import dependents
from xcraft.project import Project


def _update_ids(packages: list[str] | None, *, dry_run: bool, locked: bool, arch: str | None) -> None:
    """Update the build IDs of all the packages, for xcraft build --ids, or check them with `locked`."""
    if packages or dry_run:
        msg = "--ids updates the build IDs of all the packages: without packages, nor --dry-run"
        raise typer.BadParameter(msg)
    project = prepared_project(locked=locked, arch=arch)
    with project.lock():
        if not builder.update_build_ids(project):
            console.print("the build IDs are up to date")


def _build_remotely(
    project: Project, host: str, packages: list[str] | None, options: dict[str, bool | str], ident: str | None
) -> None:
    """Build `packages`, all by default, on the builder `host`, with the `options` of xcraft build given, then check
    that their builds came back, under the keys the builder gives them: without preparing the project here, which may
    build the image of the host, what a builder spares a slow machine.
    """
    if ident is None or options.get("--key"):
        msg = "a builder makes the builds of the developer's key: --final and --key build here, with --local"
        raise typer.BadParameter(msg)
    flags = [o if v is True else f"{o}={v}" for o, v in options.items() if v]
    # --local: on the builder, rather than on a builder of its own, if any
    args = [*(packages or []), *flags, "--dev", ident, "--arch", project.arch, "--local"]
    # all those built for the architecture by default
    keys = [] if options.get("--dry-run") else [*(packages or []), "--dev", ident, "--arch", project.arch]
    if keys:
        sign_builds(project, None, final=False)  # the developer's key, made here, for the builder to sign with it
    with project.lock():
        code, built = builders.build(project, host, args, keys)
    if code:
        raise typer.Exit(code)
    if missing := sorted(name for name, key in built.items() if not cached_build(project, name, key).exists()):
        msg = f"the builds of {', '.join(missing)} did not come back from the builder"
        raise RuntimeError(msg)


def keys(
    packages: Packages = None,
    dev: Dev = None,
    arch: Arch = None,
    recipe: Annotated[bool, typer.Option("--recipe", help="Only the hash of the recipe, without the keys.")] = False,
) -> None:
    """Print, as JSON, the hash of the recipe of the builds, and the cache keys of the builds of the packages, all by
    default, with the development builds of --dev: for the builders, see xcraft.builders.
    """
    found: dict[str, object] = {"recipe": buildkey.recipe(unprepared_project(packages, arch=arch))}
    if not recipe:
        project = prepared_project(packages, arch=arch)
        cache_keys = dev_keys(project, dev)[0]
        found["keys"] = {name: cache_keys[name] for name in packages or sorted(project.packages)}
    sys.stdout.write(json.dumps(found) + "\n")


def build(
    ctx: typer.Context,
    packages: Packages = None,
    no_deps: Annotated[bool, typer.Option("--no-deps", help="Do not build the dependencies.")] = False,
    with_dependents: Annotated[
        bool, typer.Option("--dependents", help="Also build the packages built against these ones.")
    ] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Only show what would be built.")] = False,
    ids: Annotated[
        bool,
        typer.Option(
            "--ids",
            help="Only update the build IDs of all the packages in xcraft.builds, as the builds would, without "
            "building: or check them with --locked.",
        ),
    ] = False,
    fetch: Annotated[
        builder.Fetch,
        typer.Option(
            help="What not cached locally is fetched from the remote storage, rather than built: all it has, only the "
            "builds the packages to build need, like in CI, or none.",
        ),
    ] = "all",
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Show the build logs.")] = False,
    lint_dev: Annotated[
        bool,
        typer.Option(
            "--lint",
            help="Also lint the binary packages of the development builds, as those of the final builds are: see "
            "xcraft lint.",
        ),
    ] = False,
    locked: Annotated[
        bool, typer.Option("--locked", help="Fail the builds that would change the lock, for CI.")
    ] = False,
    no_reuse: Annotated[
        bool,
        typer.Option(
            "--no-reuse",
            help="Give their own build IDs to the rebuilds the same as the builds of the main branch, rather than "
            "reusing those: when another architecture lacks them, like one being added.",
        ),
    ] = False,
    dev: Dev = None,
    final: Final = False,
    nocheck: NoCheck = False,
    arch: Arch = None,
    key: Key = None,
    jobs: Jobs = None,
    local: Annotated[
        bool,
        typer.Option("--local", help="Build on this machine, rather than on the builder of the architecture, if any."),
    ] = False,
) -> None:
    r"""Build the given packages, all by default, and what they need, in the build environment.

    The packages whose build ID is not the main branch's get development builds, unless --final, see --dev. The builds
    are signed by --key, or the default key: \[sign], or else the developer's, which xcraft makes for the development
    builds when there is none, but not for --final.

    rpmlint lints the specs of the packages to build first, and the binary packages of the final builds: its errors
    fail the final builds, and only show for the development builds.

    The builds of an architecture with a builder, see $XCRAFT_BUILDERS, are made on it, natively, from a copy of the
    checkout, and copied back, unless --local.
    """
    ident = dev_ident(ctx, dev, final=final, nocheck=nocheck)
    if ids:
        _update_ids(packages, dry_run=dry_run, locked=locked, arch=arch)
        return
    if not local and (host := builders.builders().get((project := unprepared_project(packages, arch=arch)).arch)):
        options: dict[str, bool | str] = {"--no-deps": no_deps, "--dependents": with_dependents, "--dry-run": dry_run}
        options |= {
            "--verbose": verbose,
            "--locked": locked,
            "--no-reuse": no_reuse,
            "--nocheck": nocheck,
            "--fetch": fetch,
            "--key": key or "",
            "--jobs": str(jobs or ""),
        }
        _build_remotely(project, host, packages, options, ident)
        return
    project = prepared_project(packages, locked=locked, arch=arch)
    names = list(packages or sorted(project.packages))
    if with_dependents:
        names += sorted(dependents(project.deps, names) - set(names))
    if dry_run:
        builder.build(project, names, with_deps=not no_deps, dry_run=True, dev=ident, fetch=fetch)
        return
    sign_builds(project, key, final=ident is None)
    with project.lock():
        results, keys, autorev = builder.build(
            project,
            names,
            with_deps=not no_deps,
            verbose=verbose,
            dev=ident,
            nocheck=nocheck,
            fetch=fetch,
            lint_dev=lint_dev,
            reuse=not no_reuse,
            jobs=jobs,
        )
    if dev_built := [r.name for r in results if r.name in autorev and r.status == "built"]:
        if verbose:
            for name in dev_built:
                for rpm in sorted((cached_build(project, name, keys[name]) / "RPMS").rglob("*.rpm")):
                    console.print(str(rpm), markup=False)
        hint("", f"xcraft repo {' '.join(dev_built)}", " makes a repository of these development builds, for the hosts")
    counts = Counter(r.status for r in results)
    statuses = ("built", "reused", "fetched", "cached", "stored", "failed", "skipped")
    console.print(", ".join(f"{counts[status]} {status}" for status in statuses if counts[status]))
    if any(r.failed for r in results):
        raise typer.Exit(1)
