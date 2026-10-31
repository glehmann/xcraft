"""Development builds: the release suffixes of the packages whose build ID is not the main branch's."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from xcraft.buildid import base, compute, proposed
from xcraft.buildkey import Inputs
from xcraft.cache import build_keys
from xcraft.graph import components
from xcraft.project import write_atomically

if TYPE_CHECKING:
    from collections.abc import Callable

    from xcraft.buildid import BuildId
    from xcraft.project import Project


_COUNTERS = TypeAdapter(dict[str, tuple[int, str, str]])
"""The counters of the development builds, by package and build number: the counter, and the fingerprint and the
identifier of the last one, in .xcraft/dev-builds.json, see _suffixes()."""


def _counters(project: Project) -> dict[str, tuple[int, str, str]]:
    path = project.state / "dev-builds.json"
    return _COUNTERS.validate_json(path.read_bytes()) if path.exists() else {}


def _current(
    project: Project, hashes: dict[str, str] | None = None
) -> tuple[Inputs, dict[str, BuildId], dict[str, str], set[str]]:
    """Return the inputs of the keys, the build IDs, the cache keys of the final builds, and the packages with
    development builds, computed once, with the `hashes` of the package directories, those of now by default.
    """
    fork, inputs = base(project), Inputs(project, hashes)
    ids = compute(project, fork, inputs=inputs)
    return inputs, ids, build_keys(project, ids=ids, inputs=inputs), proposed(fork, ids)


def dev_keys(
    project: Project, ident: str | None, hashes: dict[str, str] | None = None
) -> tuple[dict[str, str], dict[str, str]]:
    """Return the cache keys of the builds, see xcraft.cache.build_keys(), with the release suffixes of the
    development builds, ``~<n>.<ident>``, and those suffixes: of the packages whose build ID is not the main branch's,
    see xcraft.buildid, or none for final builds, without `ident`.

    The counter <n> of a package and build number goes up when the inputs of its build, those of the development builds
    it is built against included, or `ident` change, so that a development build upgrades the previous ones, whatever
    their identifiers, and a build of the same inputs is cached: the suffixes are those the builds have, or would have,
    until keep_built() keeps the counters of new ones. The `hashes` of the package directories are those of now by
    default.
    """
    if ident is None:
        return build_keys(project, {}, hashes), {}
    current, counters = _current(project, hashes), _counters(project)

    def suffix(_name: str, key: str, fingerprint: str) -> str:
        counter, last, last_ident = counters.get(key, (0, "", ""))
        if (last, last_ident) != (fingerprint, ident):
            counter += 1
        return f"~{counter}.{ident}"

    suffixes = _suffixes(project, current, suffix)
    return build_keys(project, suffixes, ids=current[1], inputs=current[0]), suffixes


def _suffixes(
    project: Project,
    current: tuple[Inputs, dict[str, BuildId], dict[str, str], set[str]],
    suffix: Callable[[str, str, str], str | None],
) -> dict[str, str]:
    """Return the suffixes of the development builds of `current`, see _current(), by package, those `suffix` returns
    with their name, the key of their counter and their fingerprint: the hash of their final cache key and of the cache
    keys of the packages they are built against, out of their component, with their suffixes, each chosen after those
    of the packages it is built against, rather than with its own counter.
    """
    inputs, ids, final, developed = current
    suffixes: dict[str, str] = {}
    for component in components(inputs.deps):  # the dependencies first
        if not component & developed:
            continue
        keys = build_keys(project, suffixes, ids=ids, inputs=inputs)
        for name in sorted(component & developed):
            outside = {dep: keys[dep] for dep in sorted(inputs.deps[name] - component)}
            fingerprint = hashlib.sha256(json.dumps([final[name], outside]).encode()).hexdigest()
            if (chosen := suffix(name, f"{name} {ids[name].number}", fingerprint)) is not None:
                suffixes[name] = chosen
    return suffixes


def keep_built(
    project: Project, ident: str | None, autorev: dict[str, str], names: list[str], hashes: dict[str, str]
) -> None:
    """Keep the counters of the development builds of `names`, just made with the suffixes `autorev`, for the inputs
    they have once built, with the project's generated files, across `clean`: the `hashes` of the package directories
    the builds started with, which may have changed since.
    """
    if ident is None or not (built := [name for name in names if name in autorev]):
        return
    counters = _counters(project)

    def keep(name: str, key: str, fingerprint: str) -> str | None:
        if name in built:
            counters[key] = (int(autorev[name][1:].split(".")[0]), fingerprint, ident)
        return autorev.get(name)  # as built

    _suffixes(project, _current(project, hashes), keep)
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
    """
    counters = _counters(project)

    def last(_name: str, key: str, fingerprint: str) -> str | None:
        counter, last, ident = counters.get(key, (0, "", ""))
        return f"~{counter}.{ident}" if last == fingerprint else None

    return _suffixes(project, _current(project), last)
