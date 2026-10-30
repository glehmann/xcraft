"""Development builds: the release suffixes of the packages whose build ID is not the main branch's."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from xcraft.buildid import base, compute, proposed
from xcraft.cache import build_keys
from xcraft.project import write_atomically

if TYPE_CHECKING:
    from xcraft.project import Project


def dev_suffixes(project: Project, ident: str | None) -> dict[str, str]:
    """Return the release suffixes of the development builds, ``~<n>.<ident>``: those of the packages whose build ID
    is not the main branch's, see xcraft.buildid, or none for final builds, without `ident`.

    The counter <n> of a package and build number goes up when the inputs of its build or `ident` change, so that a
    development build upgrades the previous ones, whatever their identifiers, and a build of the same inputs is
    cached: the suffixes are those the builds have, or would have, until keep_built() keeps the counters of new ones.
    """  # noqa: D205
    if ident is None:
        return {}
    path = project.state / "dev-builds.json"
    counters: dict[str, list] = json.loads(path.read_text()) if path.exists() else {}
    ids, keys = compute(project, base(project)), build_keys(project)
    suffixes = {}
    for name in sorted(proposed(project, ids)):
        entry = f"{name} {ids[name].number}"
        counter, key, last = counters.get(entry, [0, "", ""])
        if [key, last] != [keys[name], ident]:
            counter += 1
        suffixes[name] = f"~{counter}.{ident}"
    return suffixes


def keep_built(project: Project, ident: str | None, autorev: dict[str, str], names: list[str]) -> None:
    """Keep the counters of the development builds of `names`, just made with the suffixes `autorev`, for the inputs
    they have once built, with the project's generated files, across `clean`.
    """  # noqa: D205
    if not (built := [name for name in names if name in autorev]):
        return
    path = project.state / "dev-builds.json"
    counters: dict[str, list] = json.loads(path.read_text()) if path.exists() else {}
    ids, keys = compute(project, base(project)), build_keys(project)
    for name in built:
        counters[f"{name} {ids[name].number}"] = [int(autorev[name][1:].split(".")[0]), keys[name], ident]
    path.parent.mkdir(exist_ok=True)
    write_atomically(path, json.dumps(counters, indent=1, sort_keys=True) + "\n")


def developed(project: Project) -> set[str]:
    """Return the packages whose build ID is not the main branch's: their builds are development builds."""
    return proposed(project, compute(project, base(project)))


def built_dev_suffixes(project: Project) -> dict[str, str]:
    """Return the release suffixes of the development builds, as last built: those whose inputs did not change since,
    whatever their identifiers.
    """  # noqa: D205
    path = project.state / "dev-builds.json"
    counters: dict[str, list] = json.loads(path.read_text()) if path.exists() else {}
    ids, keys = compute(project, base(project)), build_keys(project)
    return {
        name: f"~{counter}.{ident}"
        for name in proposed(project, ids)
        for counter, key, ident in [counters.get(f"{name} {ids[name].number}", [0, "", ""])]
        if key == keys[name]
    }
