"""Development builds: the release suffixes of the packages whose build ID is not the main branch's."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from xcraft.buildid import base, compute, proposed
from xcraft.cache import build_keys
from xcraft.project import write_atomically

if TYPE_CHECKING:
    from xcraft.buildid import BuildId
    from xcraft.project import Project


_COUNTERS = TypeAdapter(dict[str, tuple[int, str, str]])
"""The counters of the development builds, by package and build number: the counter, and the key and the identifier
of the last one, in .xcraft/dev-builds.json."""


def _counters(project: Project) -> dict[str, tuple[int, str, str]]:
    path = project.state / "dev-builds.json"
    return _COUNTERS.validate_json(path.read_bytes()) if path.exists() else {}


def _current(project: Project) -> tuple[dict[str, BuildId], dict[str, str], set[str]]:
    """Return the build IDs, the cache keys of the final builds, and the packages with development builds, computed
    once.
    """  # noqa: D205
    fork = base(project)
    ids = compute(project, fork)
    return ids, build_keys(project, ids=ids), proposed(fork, ids)


def dev_suffixes(project: Project, ident: str | None) -> dict[str, str]:
    """Return the release suffixes of the development builds, ``~<n>.<ident>``: those of the packages whose build ID
    is not the main branch's, see xcraft.buildid, or none for final builds, without `ident`.

    The counter <n> of a package and build number goes up when the inputs of its build or `ident` change, so that a
    development build upgrades the previous ones, whatever their identifiers, and a build of the same inputs is
    cached: the suffixes are those the builds have, or would have, until keep_built() keeps the counters of new ones.
    """  # noqa: D205
    if ident is None:
        return {}
    counters, (ids, keys, developed) = _counters(project), _current(project)
    suffixes = {}
    for name in sorted(developed):
        counter, key, last = counters.get(f"{name} {ids[name].number}", (0, "", ""))
        if (key, last) != (keys[name], ident):
            counter += 1
        suffixes[name] = f"~{counter}.{ident}"
    return suffixes


def keep_built(project: Project, ident: str | None, autorev: dict[str, str], names: list[str]) -> None:
    """Keep the counters of the development builds of `names`, just made with the suffixes `autorev`, for the inputs
    they have once built, with the project's generated files, across `clean`.
    """  # noqa: D205
    if ident is None or not (built := [name for name in names if name in autorev]):
        return
    counters, (ids, keys, _) = _counters(project), _current(project)
    for name in built:
        counters[f"{name} {ids[name].number}"] = (int(autorev[name][1:].split(".")[0]), keys[name], ident)
    project.state.mkdir(exist_ok=True)
    write_atomically(
        project.state / "dev-builds.json", _COUNTERS.dump_json(dict(sorted(counters.items())), indent=1).decode() + "\n"
    )


def developed(project: Project) -> set[str]:
    """Return the packages whose build ID is not the main branch's: their builds are development builds."""
    fork = base(project)
    return proposed(fork, compute(project, fork))


def built_dev_suffixes(project: Project) -> dict[str, str]:
    """Return the release suffixes of the development builds, as last built: those whose inputs did not change since,
    whatever their identifiers.
    """  # noqa: D205
    counters, (ids, keys, developed) = _counters(project), _current(project)
    return {
        name: f"~{counter}.{ident}"
        for name in developed
        for counter, key, ident in [counters.get(f"{name} {ids[name].number}", (0, "", ""))]
        if key == keys[name]
    }
