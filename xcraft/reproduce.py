"""Building packages again, to check that their builds are reproducible: the same packages from the same inputs."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from xcraft import build as builder
from xcraft import ccache
from xcraft.cache import BuildError, cached_build, cached_builds, release_suffix
from xcraft.graph import build_order

if TYPE_CHECKING:
    from pathlib import Path

    from xcraft.project import Project

DIR = "reproduce"
"""The directory of the project's state keeping the rebuilds which differ from their builds, by package."""


def reproduce(
    project: Project, names: list[str], *, dev: str | None, nocheck: bool, verbose: bool, with_caches: bool = False
) -> dict[str, list[str]]:
    """Build the packages `names` again, each alone, against the same builds of their dependencies, without their
    compiler caches, but `with_caches`, and return how the rebuild of each differs from its build, see build.compare():
    nothing when the same. With the caches, what they cache is the same by construction, copied from them: only the
    rest is checked, like the links and the packaging, quickly.

    Their builds, and those of what they need, are made or fetched first, like xcraft build makes them, with the
    development builds `dev`, if any, and without the tests of their specs with `nocheck`: they stay, and the rebuilds
    are dropped, but those which differ, kept in .xcraft/reproduce/<name>/again, to see how.
    """
    results, keys, _ = builder.build(project, names, dev=dev, nocheck=nocheck, verbose=verbose)
    if failed := sorted(r.name for r in results if r.failed):
        msg = f"not built: {', '.join(failed)}"
        raise BuildError(msg)
    return {
        name: _again(project, name, keys[name], dev=dev, nocheck=nocheck, verbose=verbose, with_caches=with_caches)
        for name in build_order(project.deps, names, with_deps=False)
    }


def _again(
    project: Project, name: str, key: str, *, dev: str | None, nocheck: bool, verbose: bool, with_caches: bool
) -> list[str]:
    """Build `name` again with `key`, its builds of all the signers and its compiler caches, but `with_caches`, set
    aside, and return how
    it differs from the build of `key`: the rebuild, compared, is dropped, or kept when it differs, and all that was
    set aside is put back.
    """
    if not (builds := cached_builds(project, name, key)):
        msg = f"no build of {name} to compare with: {release_suffix(key)}"
        raise BuildError(msg)
    work = project.state / DIR / name
    shutil.rmtree(work, ignore_errors=True)
    caches = [] if with_caches else [ccache.local_path(project, name)]
    aside: list[tuple[Path, Path]] = []
    try:
        for i, path in enumerate([*builds, *caches]):
            if path.exists():
                (moved := work / "aside" / str(i)).parent.mkdir(parents=True, exist_ok=True)
                aside.append((path, path.rename(moved)))
        built = aside[0][1]
        # alone, from the builds of its dependencies, cached: neither fetched, nor reused, nor their compiler caches
        results, keys, _ = builder.build(
            project,
            [name],
            with_deps=False,
            dev=dev,
            nocheck=nocheck,
            verbose=verbose,
            fetch="none",
            reuse=False,
            dune_cache=with_caches,
        )
        if any(r.failed for r in results):
            return ["its build failed again"]
        if keys[name] != key:
            return [f"what its build recorded in the lock changed, and its key: {release_suffix(keys[name])}"]
        again = cached_build(project, name, key)
        if found := builder.compare(project, name, (built, release_suffix(key)), (again, release_suffix(key))):
            again.rename(work / "again")
        return found
    finally:
        for path in [*cached_builds(project, name, key), *caches]:
            shutil.rmtree(path, ignore_errors=True)  # the rebuild, and its compiler caches
        for path, moved in aside:
            moved.rename(path)
        shutil.rmtree(work / "aside", ignore_errors=True)
